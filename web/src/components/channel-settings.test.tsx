// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChannelsSection } from "@/components/channel-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("Lark bot-token settings", () => {
  let container: HTMLDivElement;
  let root: Root;
  let bot: { name: string; app_id: string; open_id: string; profile: string } | null;

  beforeEach(() => {
    bot = { name: "Example bot", app_id: "cli_example", open_id: "ou_botexample", profile: "" };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/channel_configs/list") return Response.json({ status: "ok", result: { configs: [{
        channel_id: "channel-lark_bot", channel_type: "lark_bot", display_name: "Lark (bot token)",
        enabled: true, has_bot_token: true, created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z",
        config: { app_id: "cli_example", domain: "feishu", allowed_open_ids: ["ou_example123"], allowlist_app_id: "cli_example", description: "" },
      }] } });
      if (url === "/api/v1/channels/status") return Response.json({ status: "ok", result: {
        bot, problem: bot ? null : "the platform rejected the app credentials: invalid secret", names: { ou_example123: "Allowed person" },
        rejected: [{ open_id: "ou_refused123", name: "Refused person", count: 1, last_at: "2026-10-18T00:00:00Z", chat_id: null }],
      } });
      if (url === "/api/v1/channels/people") return Response.json({ status: "ok", result: { people: [], complete: false } });
      if (url === "/api/v1/gate_settings/get") return Response.json({ status: "ok", result: { targets: [], dashboard_target: null } });
      throw new Error(`Unexpected request: ${url}`);
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

  it("shows the bot app without a profile, masked secret, sender names and incomplete search warning", async () => {
    await act(async () => root.render(<ChannelsSection />));
    expect(container.textContent).toContain("Example bot (cli_example)");
    expect(container.textContent).not.toContain("profile");
    expect(container.textContent).toContain("Allowed person");
    expect(container.textContent).toContain("Refused person");
    const label = [...container.querySelectorAll("label")].find((node) => node.textContent === "App secret")!;
    const secret = document.getElementById(label.htmlFor) as HTMLInputElement;
    expect(secret.type).toBe("password");
    expect(secret.value).toBe("***");
    expect(container.querySelector('input[placeholder="cli_…"]')).not.toBeNull();
    expect(container.querySelector('input[placeholder="feishu"]')).not.toBeNull();
    const input = container.querySelector('input[placeholder="Name or email"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "Example");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    });
    expect(container.textContent).toContain("Nobody found");
    expect(container.textContent).toContain("Only part of the directory was searched: use the person's email.");
  });

  it("explains when the platform rejects the app credentials", async () => {
    bot = null;
    await act(async () => root.render(<ChannelsSection />));
    expect(container.textContent).toContain("Bot: unknown, the platform rejected the app credentials");
  });
});
