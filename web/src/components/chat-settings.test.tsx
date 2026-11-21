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
  let stored: { stop_enabled: boolean; completion_turns: string; ledger_enabled: boolean };
  let missing: { channel_id: string; channel_type: string; name: string }[];
  let summary: Record<string, unknown> | null;
  let puts: unknown[];
  let outbound: boolean;
  let outboundError: boolean;

  beforeEach(() => {
    stored = { stop_enabled: false, completion_turns: "off", ledger_enabled: false };
    missing = [];
    summary = SUMMARY;
    puts = [];
    outbound = false;
    outboundError = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.startsWith("/api/v1/outbound_gateway/")) {
        if (outboundError) return Response.json({ status: "error", error: { code: "unavailable", message: "gateway unavailable" } }, { status: 503 });
        if (path.endsWith("/update")) {
          const body = JSON.parse(String(init?.body));
          puts.push({ operation: "outbound_gateway.update", ...body });
          outbound = body.outbound_enabled;
        }
        return Response.json({ status: "ok", result: { outbound_enabled: outbound, send_qps: 5, write_qps: 15, edit_interval_seconds: 1, max_in_flight: 4 } });
      }
      if (path === "/api/v1/manager_events/summary") {
        if (summary === null) {
          return Response.json({ status: "error", error: { code: "forbidden", message: "operator only" } },
                               { status: 403 });
        }
        return Response.json({ status: "ok", result: summary });
      }
      if (!["/api/v1/chat_settings/get", "/api/v1/chat_settings/update"].includes(path)) {
        throw new Error(`Unexpected request: ${input}`);
      }
      let body: { ledger_enabled?: boolean } | null = null;
      if (path === "/api/v1/chat_settings/update") {
        body = JSON.parse(String(init?.body));
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

  it("shows Stop off with an experimental note, and turns it on", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const box = container.querySelector("input[type=checkbox]") as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(container.textContent).toContain("Experimental");
    await act(async () => box.click());
    expect(puts).toEqual([{ stop_enabled: true }]);
    expect((container.querySelector("input[type=checkbox]") as HTMLInputElement).checked).toBe(true);
  });

  it("enables outbound retries without changing the other chat settings", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const box = container.querySelector("input[id$=-outbound]") as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(container.textContent).toContain("出站统一限流和重试（飞书、Telegram）");
    await act(async () => box.click());
    expect(puts).toEqual([{ operation: "outbound_gateway.update", outbound_enabled: true }]);
    expect(stored).not.toHaveProperty("outbound_enabled");
    expect(box.checked).toBe(true);
  });

  it("keeps chat settings usable when the outbound gateway is unavailable", async () => {
    outboundError = true;
    await act(async () => root.render(<ChatSettingsSection />));
    expect(container.textContent).toContain("gateway unavailable");
    expect(container.querySelector("input[id$=-outbound]")).toBeNull();
    const box = container.querySelector("input[id$=-stop]") as HTMLInputElement;
    await act(async () => box.click());
    expect(puts).toEqual([{ stop_enabled: true }]);
  });

  it("retains the outbound value after an update fails", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    outboundError = true;
    const box = container.querySelector("input[id$=-outbound]") as HTMLInputElement;
    await act(async () => box.click());
    expect(box.checked).toBe(false);
    expect(container.textContent).toContain("gateway unavailable");
    expect(puts).toEqual([]);
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
    const ledger = container.querySelector("input[id$=-ledger]") as HTMLInputElement;
    expect(ledger.checked).toBe(false);
    await act(async () => ledger.click());
    expect(puts).toEqual([{ ledger_enabled: true }]);
    const notice = container.querySelector("[role=status]") as HTMLElement;
    expect(notice.textContent).toContain(LEDGER_NOTICE);
    expect(notice.querySelector("a")?.getAttribute("href")).toBe("#settings/channels");
    expect(notice.querySelector("li")?.textContent).toBe("Example Lark");
    expect(notice.textContent).not.toContain("channel-lark");
  });

  it("lists a bot by its channel id when the display name is empty", async () => {
    missing = [{ channel_id: "channel-lark", channel_type: "lark", name: "" }];
    await act(async () => root.render(<ChatSettingsSection />));
    const ledger = container.querySelector("input[id$=-ledger]") as HTMLInputElement;
    await act(async () => ledger.click());
    const item = container.querySelector("[role=status] li");
    expect(item?.textContent).toBe("channel-lark");
  });

  it("shows no notice when turning the ledger on lists no bots", async () => {
    await act(async () => root.render(<ChatSettingsSection />));
    const ledger = container.querySelector("input[id$=-ledger]") as HTMLInputElement;
    await act(async () => ledger.click());
    expect(puts).toEqual([{ ledger_enabled: true }]);
    expect(container.querySelector("[role=status]")).toBeNull();
    expect(container.textContent).not.toContain(LEDGER_NOTICE);
  });
});
