// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChannelsSection } from "@/components/channel-settings";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const row = (channel_id: string, channel_type: string, display_name: string, extra: Record<string, unknown> = {}) => ({
  channel_id, channel_type, display_name, enabled: false, has_bot_token: false, builtin: true,
  manager_id: "manager", manager_name: "Manager", created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z",
  config: channel_type === "lark" ? { profile: "" } : {}, ...extra,
});
const BUILTIN = [
  row("channel-lark_bot", "lark_bot", "Lark (bot token)"),
  row("channel-telegram", "telegram", "Telegram"),
  row("channel-lark", "lark", "Lark (lark-cli)"),
];
const ADDED_TG = row("channel-telegram-2", "telegram", "Review bot", { builtin: false, manager_id: "manager-review", manager_name: "Code review" });
const ADDED_LARK = row("channel-lark-5", "lark", "Lark five", { builtin: false, manager_id: "manager-review", manager_name: "Code review" });

describe("Managers' bots in Settings → Channels", () => {
  let container: HTMLDivElement;
  let root: Root;
  let configs: unknown[];
  let calls: { url: string; method: string; body?: string }[];
  let postResponse: () => Response;

  beforeEach(() => {
    configs = BUILTIN;
    calls = [];
    postResponse = () => Response.json({ ...ADDED_TG }, { status: 201 });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ url, method, body: init?.body as string | undefined });
      if (url === "/api/channel-configs" && method === "GET") return Response.json({ configs });
      if (url === "/api/channel-configs" && method === "POST") return postResponse();
      if (url.startsWith("/api/channel-configs/") && method === "DELETE") return new Response(null, { status: 204 });
      if (url === "/api/managers") return Response.json({ managers: [
        { id: "manager", name: "Manager", enabled: true },
        { id: "manager-review", name: "Code review", enabled: true },
      ], max_enabled: 8 });
      if (url.startsWith("/api/channels/lark_bot/status")) return Response.json({ bot: null, problem: null, names: {}, rejected: [] });
      if (url.startsWith("/api/channels/lark/status")) return Response.json({ bot: null, problem: null, names: {}, rejected: [] });
      if (url === "/api/gate-settings") return Response.json({ targets: [], dashboard_target: null });
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

  const render = () => act(async () => root.render(<ChannelsSection />));
  const titles = () => [...container.querySelectorAll("h3")].map((h) => h.textContent);
  // Within the open add form when there is one (its heading starts "Add a"), else the whole page.
  const scope = (): ParentNode =>
    [...container.querySelectorAll("h3")].find((h) => h.textContent?.startsWith("Add a"))?.closest("div.rounded-lg") ?? container;
  const button = (text: string) => [...scope().querySelectorAll("button")].find((b) => b.textContent?.trim() === text)!;
  const click = (el: Element) => act(async () => { (el as HTMLElement).click(); });
  const type = (el: HTMLInputElement | HTMLSelectElement, value: string) =>
    act(async () => {
      const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
      el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
    });
  const field = (label: string) => {
    const l = [...scope().querySelectorAll("label")].find((n) => n.textContent?.trim() === label)!;
    return document.getElementById(l.htmlFor) as HTMLInputElement & HTMLSelectElement;
  };

  it("shows only the three built-in cards, in order, with add buttons", async () => {
    await render();
    expect(titles().slice(0, 3)).toEqual(["Telegram", "Lark (lark-cli)", "Lark (bot token)"]);
    expect(container.textContent).not.toContain("Talks to");
    expect(button("Remove")).toBeUndefined();
    expect(["Add a Telegram bot", "Add a Lark (lark-cli) bot", "Add a Lark (bot token) bot"].map((t) => !!button(t))).toEqual([true, true, true]);
  });

  it("puts an added bot after its built-in card with its Manager and a Remove button", async () => {
    configs = [...BUILTIN, ADDED_TG];
    await render();
    expect(titles().slice(0, 4)).toEqual(["Telegram", "Review bot", "Lark (lark-cli)", "Lark (bot token)"]);
    expect(container.textContent).toContain("Talks to Code review");
    expect([...container.querySelectorAll("button")].filter((b) => b.textContent?.trim() === "Remove")).toHaveLength(1);
  });

  async function fillTelegramForm() {
    await render();
    await click(button("Add a Telegram bot"));
    await type(field("Manager"), "manager-review");
    await type(field("Bot token"), "123456:SECRET");
    await click(button("Add sender"));
    await type(scope().querySelector('input[placeholder="123456789"]') as HTMLInputElement, "42");
  }

  it("creates a bot and reloads the list", async () => {
    await fillTelegramForm();
    configs = [...BUILTIN, ADDED_TG];
    await click(button("Save"));
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(JSON.parse(posts[0].body!)).toEqual({
      channel_type: "telegram", manager_id: "manager-review", bot_token: "123456:SECRET", enabled: false,
      config: { allowed_user_ids: ["42"] },
    });
    expect(calls.filter((c) => c.url === "/api/channel-configs" && c.method === "GET")).toHaveLength(2);
    expect(container.textContent).toContain("Saved — restart to apply");
    expect(container.textContent).not.toContain("123456:SECRET");
  });

  it("shows the server's refusal and clears the secret without reloading", async () => {
    postResponse = () => Response.json({ error: "Manager is deleted" }, { status: 409 });
    await fillTelegramForm();
    await click(button("Save"));
    expect(container.textContent).toContain("Manager is deleted");
    expect(field("Bot token").value).toBe("");
    expect(field("Manager").value).toBe("manager-review");
    expect(calls.filter((c) => c.url === "/api/channel-configs" && c.method === "GET")).toHaveLength(1);
  });

  it("removes a bot only after confirming", async () => {
    configs = [...BUILTIN, ADDED_TG];
    await render();
    const confirmMock = vi.fn(() => false);
    vi.stubGlobal("confirm", confirmMock);
    await click(button("Remove"));
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    confirmMock.mockReturnValue(true);
    await click(button("Remove"));
    expect(confirmMock).toHaveBeenCalledWith("Remove Review bot? Its conversations stay stored; it keeps running until the server restarts.");
    expect(calls.filter((c) => c.method === "DELETE").map((c) => c.url)).toEqual(["/api/channel-configs/channel-telegram-2"]);
    expect(calls.filter((c) => c.url === "/api/channel-configs" && c.method === "GET")).toHaveLength(2);
  });

  it("asks for a bot's Lark status by channel id, the built-in row without", async () => {
    configs = [...BUILTIN, ADDED_LARK];
    await render();
    const urls = calls.map((c) => c.url);
    expect(urls).toContain("/api/channels/lark/status?channel_id=channel-lark-5");
    expect(urls).toContain("/api/channels/lark/status");
  });
});
