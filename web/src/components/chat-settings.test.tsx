// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatSettingsSection } from "@/components/chat-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SUMMARY = {
  window_hours: 24, total: 6, by_state: { delivered: 3, fallback: 2, skipped: 1 },
  by_reason: { turn_timeout: 1, queue_deadline: 1, group_chat: 1 }, turn_cost_usd: 0.0412, pending: 0,
  oldest_pending_seconds: null, failing: false,
};

const LEDGER_NOTICE = "这些机器人的私聊里，你做的决定会记成转述，直到你在 Settings → Channels 填上『我的 ID』";

describe("Chat settings", () => {
  let container: HTMLDivElement;
  let root: Root;
  let stored: Record<string, unknown>;
  let context: Record<string, unknown>;
  let missing: { channel_id: string; channel_type: string; name: string }[];
  let summary: Record<string, unknown> | null;
  let puts: unknown[];
  let metrics = false;
  let metricsError = false;
  let metricsPuts: unknown[];

  beforeEach(() => {
    stored = { stop_enabled: false, completion_turns: "off", ledger_enabled: false };
    context = { turn_context_enabled: false, timezone: null, server_timezone: "America/Los_Angeles",
      reply_language: "zh-CN", missing_operator_ids: [] };
    missing = [];
    summary = SUMMARY;
    puts = [];
    metrics = false;
    metricsError = false;
    metricsPuts = [];
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.startsWith("/api/v1/manager_metrics_settings/")) {
        if (path.endsWith("/update")) {
          metricsPuts.push(JSON.parse(String(init?.body)));
          if (metricsError) return Response.json({ status: "error", error: { code: "unavailable", message: "Save failed" } }, { status: 503 });
          metrics = JSON.parse(String(init?.body)).enabled;
        }
        return Response.json({ status: "ok", result: { enabled: metrics } });
      }
      if (path === "/api/v1/manager_events/summary") {
        if (summary === null) {
          return Response.json({ status: "error", error: { code: "forbidden", message: "operator only" } },
                               { status: 403 });
        }
        return Response.json({ status: "ok", result: summary });
      }
      if (path.startsWith("/api/v1/turn_context_settings/")) {
        if (path.endsWith("/update")) {
          const body = JSON.parse(String(init?.body));
          expect(Object.keys(body).every((key) => ["turn_context_enabled", "timezone", "reply_language"].includes(key))).toBe(true);
          puts.push(body);
          context = { ...context, ...body };
        }
        return Response.json({ status: "ok", result: context });
      }
      if (!["/api/v1/chat_settings/get", "/api/v1/chat_settings/update"].includes(path)) {
        throw new Error(`Unexpected request: ${input}`);
      }
      let body: { ledger_enabled?: boolean } | null = null;
      if (path === "/api/v1/chat_settings/update") {
        body = JSON.parse(String(init?.body));
        expect(Object.keys(body!).every((key) => ["stop_enabled", "completion_turns", "ledger_enabled"].includes(key))).toBe(true);
        puts.push(body);
        stored = { ...stored, ...body };
      }
      const result = { ...stored, ...(body?.ledger_enabled === true ? { missing_operator_ids: missing } : {}) };
      return Response.json({ status: "ok", result });
    }));
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it("saves metrics through its own operator setting and preserves it across chat writes", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const box = container.querySelector("input[id$='-metrics']") as HTMLInputElement;
    expect(box).not.toBeNull();
    expect(box.checked).toBe(false);
    await act(async () => box.click());
    expect(metricsPuts).toEqual([{ enabled: true }]);
    expect(puts).toEqual([]);
    await act(async () => (container.querySelector("input[id$='-stop']") as HTMLInputElement).click());
    expect(puts).toEqual([{ stop_enabled: true }]);
    expect(box.checked).toBe(true);
    await act(async () => (container.querySelector("input[aria-label='每轮上下文']") as HTMLInputElement).click());
    expect(puts).toEqual([{ stop_enabled: true }, { turn_context_enabled: true }]);
    expect(box.checked).toBe(true);
    expect(metricsPuts).toEqual([{ enabled: true }]);
  });

  it("names the metrics switch the way the dashboard's off line points to it", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const box = container.querySelector("input[id$='-metrics']") as HTMLInputElement;
    expect(box.closest("label")!.textContent).toBe("记录管家体验指标");
  });

  it("keeps the metrics value when a change is cancelled or fails", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const box = container.querySelector("input[id$='-metrics']") as HTMLInputElement;
    expect(box).not.toBeNull();
    vi.mocked(confirm).mockReturnValue(false);
    await act(async () => box.click());
    expect(metricsPuts).toEqual([]);
    expect(box.checked).toBe(false);
    vi.mocked(confirm).mockReturnValue(true);
    metricsError = true;
    await act(async () => box.click());
    expect(box.checked).toBe(false);
    expect(container.querySelector("[role=alert]")?.textContent).toContain("Save failed");
  });

  it("shows Stop off with an experimental note, and turns it on", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const box = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(container.textContent).toContain("Experimental");
    await act(async () => box.click());
    expect(puts).toEqual([{ stop_enabled: true }]);
    expect((container.querySelector("input[type=checkbox]") as HTMLInputElement).checked).toBe(true);
  });

  it("switches the Manager's completion turns on alone, and shows the day's counts", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const select = container.querySelector("select") as HTMLSelectElement;
    expect(select.value).toBe("off");
    expect(container.textContent).toContain("3 told by the Manager");
    expect(container.textContent).toContain("2 sent as the notice after a turn (it took over 2 minutes 1, waited 15 minutes 1)");
    expect(container.textContent).toContain("1 notice without a turn");
    expect(container.textContent).toContain("$0.04 on turns");
    expect(container.textContent).not.toContain("fell back");
    await act(async () => {
      select.value = "dm_only";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(puts).toEqual([{ completion_turns: "dm_only" }]);
    expect((container.querySelector("select") as HTMLSelectElement).value).toBe("dm_only");
  });

  it("enables turn context alone and lists bots that cannot identify the operator", async () => {
    context.missing_operator_ids = [{ channel_id: "channel-example", name: "Example bot", channel_type: "lark" }];
    await act(async () => root.render(<ChatSettingsSection />));
    const box = container.querySelector("input[aria-label='每轮上下文']") as HTMLInputElement;
    expect(box.checked).toBe(false);
    await act(async () => box.click());
    expect(puts).toEqual([{ turn_context_enabled: true }]);
    expect(container.textContent).toContain("Example bot");
    expect(container.textContent).toContain("需要操作者授权的动作会被拒绝");
    const ledger = container.querySelector("input[aria-describedby$='-ledger-help']") as HTMLInputElement;
    await act(async () => ledger.click());
    expect(box.checked).toBe(true);
    const language = container.querySelector("select[aria-label='回复语言']") as HTMLSelectElement;
    await act(async () => {
      language.value = "en";
      language.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(puts).toEqual([{ turn_context_enabled: true }, { ledger_enabled: true }, { reply_language: "en" }]);
    expect(box.checked).toBe(true);
    expect(ledger.checked).toBe(true);
    expect(container.textContent).toContain("Example bot");
  });

  it("saves the time zone, clears it to the server default, and saves the reply language alone", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const zone = container.querySelector("input[aria-label='时区']") as HTMLInputElement;
    expect(zone.placeholder).toBe("America/Los_Angeles");
    const setInput = (value: string) => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(zone, value);
      zone.dispatchEvent(new Event("input", { bubbles: true }));
    };
    await act(async () => setInput("Asia/Shanghai"));
    await act(async () => (container.querySelector("button[type=submit]") as HTMLButtonElement).click());
    expect(puts).toEqual([{ timezone: "Asia/Shanghai" }]);
    await act(async () => setInput(""));
    await act(async () => (container.querySelector("button[type=submit]") as HTMLButtonElement).click());
    expect(puts.at(-1)).toEqual({ timezone: null });
    const language = container.querySelector("select[aria-label='回复语言']") as HTMLSelectElement;
    await act(async () => {
      language.value = "en";
      language.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(puts.at(-1)).toEqual({ reply_language: "en" });
  });

  it("warns when the last turns all fell back, and says when the counts cannot be read", async () => {
    summary = { ...SUMMARY, failing: true };
    await act(async () => root.render(<ChatSettingsSection />));
    expect(container.textContent).toContain("The last 5 turns all fell back to the notice");
    act(() => root.unmount());
    summary = null;
    root = createRoot(container);
    await act(async () => root.render(<ChatSettingsSection />));
    expect(container.textContent).toContain("Task-end counts are unavailable");
    expect((container.querySelector("select") as HTMLSelectElement).value).toBe("off");
  });

  it("turns the ledger on and names the bots that have no operator id", async () => {
    missing = [{ channel_id: "channel-lark", channel_type: "lark", name: "Example Lark" }];
    await act(async () => root.render(<ChatSettingsSection />));
    const ledger = container.querySelector("input[aria-describedby$='-ledger-help']") as HTMLInputElement;
    expect(ledger.checked).toBe(false);
    await act(async () => ledger.click());
    expect(puts).toEqual([{ ledger_enabled: true }]);
    const notice = container.querySelector("[role=status]") as HTMLElement;
    expect(notice.textContent).toContain(LEDGER_NOTICE);
    expect(notice.querySelector("a")?.getAttribute("href")).toBe("#settings/channels");
    expect(notice.querySelector("li")?.textContent).toBe("Example Lark");
    expect(notice.textContent).not.toContain("channel-lark");
  });

  it("saves turn context independently while keeping the ledger enabled", async () => {
    stored.ledger_enabled = true;
    await act(async () => root.render(<ChatSettingsSection />));
    const context = container.querySelector("input[aria-label='每轮上下文']") as HTMLInputElement;
    await act(async () => context.click());
    expect(puts).toEqual([{ turn_context_enabled: true }]);
    expect((container.querySelector("input[aria-describedby$='-ledger-help']") as HTMLInputElement).checked).toBe(true);
    expect(context.checked).toBe(true);
  });

  it("lists a bot by its channel id when the display name is empty", async () => {
    missing = [{ channel_id: "channel-lark", channel_type: "lark", name: "" }];
    await act(async () => root.render(<ChatSettingsSection />));
    const ledger = container.querySelector("input[aria-describedby$='-ledger-help']") as HTMLInputElement;
    await act(async () => ledger.click());
    const item = container.querySelector("[role=status] li");
    expect(item?.textContent).toBe("channel-lark");
  });

  it("shows no notice when turning the ledger on lists no bots", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const ledger = container.querySelector("input[aria-describedby$='-ledger-help']") as HTMLInputElement;
    await act(async () => ledger.click());
    expect(puts).toEqual([{ ledger_enabled: true }]);
    expect(container.querySelector("[role=status]")).toBeNull();
    expect(container.textContent).not.toContain(LEDGER_NOTICE);
  });
});
