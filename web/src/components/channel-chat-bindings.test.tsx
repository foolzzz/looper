// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChannelChatBindings } from "./channel-chat-bindings";
import type { Binding, ChannelChats, SeenChat } from "@/lib/chat-bindings-api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const owner = { id: "manager", name: "Owner", enabled: true };
const other = { id: "manager-review", name: "Review", enabled: true };
const off = { id: "manager-off", name: "Paused", enabled: false };
const stamp = "2026-01-01T00:00:00Z";
const ok = (result: unknown) => Response.json({ status: "ok", result });
const refused = (status = 409, code = "conflict") => Response.json({ status: "error", error: { code, message: "Synthetic refusal" } }, { status });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}
function chat(chat_id = "42", name: string | null = "Sample chat"): SeenChat {
  return { chat_id, name, count: 2, last_at: stamp, binding: null, effective_manager_id: owner.id,
    effective_manager_name: owner.name, effective_manager_enabled: true, effective_manager_deleted: false };
}
function binding(channel_id: string, chat_id: string, manager_id: string): Binding {
  const manager = managers.find((m) => m.id === manager_id) ?? off;
  return { channel_id, chat_id, manager_id, created_at: stamp, channel_type: "telegram", bot_name: "Example bot",
    chat_name: null, manager_name: manager.name, manager_enabled: manager.enabled, manager_deleted: false };
}
let host: HTMLDivElement, root: Root;
let managers: typeof owner[];
let catalogs: Record<string, SeenChat[]>;
let policies: Record<string, boolean>;
let bindings: Binding[];
let calls: { path: string; body: Record<string, unknown>; signal?: AbortSignal | null }[];
let intercept: (path: string, body: Record<string, unknown>) => Response | Promise<Response> | undefined;
let pageResult: ((channel: string, after?: string) => ChannelChats) | undefined;
function snapshot(channel_id: string): ChannelChats {
  return { channel_id, bot_manager_id: owner.id, bound_chats_only: policies[channel_id] ?? false,
    chats: (catalogs[channel_id] ?? [chat()]).map((c) => {
      const b = bindings.find((b) => b.channel_id === channel_id && b.chat_id === c.chat_id) ?? null;
      return { ...c, binding: b, effective_manager_id: b?.manager_id ?? owner.id,
        effective_manager_name: b?.manager_name ?? owner.name, effective_manager_enabled: b?.manager_enabled ?? true,
        effective_manager_deleted: b?.manager_deleted ?? false };
    }), unbound_chats: [], next_cursor: null, has_more: false };
}
beforeEach(() => {
  managers = [owner, other, off]; catalogs = {}; policies = {}; bindings = []; calls = [];
  intercept = () => undefined; pageResult = undefined;
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url).replace("/api/v1/", "");
    const body = JSON.parse(String(init?.body ?? "{}"));
    calls.push({ path, body, signal: init?.signal });
    const custom = intercept(path, body);
    if (custom) return custom;
    const channel = body.channel_id as string;
    if (path === "managers/list") return ok({ managers, max_enabled: 8 });
    if (path === "channel_chats/list") return ok(pageResult?.(channel, body.after) ?? snapshot(channel));
    if (path === "manager_chat_bindings/list") return ok({ bindings: bindings.filter((b) => b.channel_id === channel) });
    if (path === "manager_chat_bindings/put") {
      bindings = bindings.filter((b) => b.channel_id !== channel || b.chat_id !== body.chat_id);
      const b = binding(channel, body.chat_id as string, body.manager_id as string); bindings.push(b);
      return ok({ binding: b });
    }
    if (path === "manager_chat_bindings/delete") {
      bindings = bindings.filter((b) => b.channel_id !== channel || b.chat_id !== body.chat_id);
      return ok({ ...body, deleted: true });
    }
    if (path === "channel_chats/set_policy") { policies[channel] = body.bound_chats_only as boolean; return ok(body); }
    throw new Error(`Unexpected path ${path}`);
  }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); });
const render = (channelId = "channel-telegram") => act(async () => root.render(<ChannelChatBindings channelId={channelId} botManagerId="manager" botManagerName="Owner" />));
const select = (index = 0) => host.querySelectorAll("select")[index];
const change = (value: string, index = 0) => act(async () => {
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select(index), value);
  select(index).dispatchEvent(new Event("change", { bubbles: true }));
});
const button = (name: string) => [...host.querySelectorAll("button")].find((b) => b.textContent === name)!;
const click = (element: HTMLElement) => act(async () => element.click());
const writes = (suffix: string) => calls.filter((c) => c.path.endsWith(suffix));

it("scopes every catalog and binding read to the bot identity", async () => {
  await render("channel-telegram-2");
  expect(calls.filter((c) => c.path !== "managers/list").map((c) => c.body.channel_id)).toEqual(["channel-telegram-2", "channel-telegram-2"]);
});
it("isolates two bots sharing the same native chat id", async () => {
  await act(async () => root.render(<><ChannelChatBindings channelId="channel-telegram" botManagerId="manager" /><ChannelChatBindings channelId="channel-telegram-2" botManagerId="manager" /></>));
  await change(other.id);
  expect(select(0).value).toBe(other.id); expect(select(1).value).toBe("");
  expect(writes("/put")[0].body).toEqual({ channel_id: "channel-telegram", chat_id: "42", manager_id: other.id });
});
it("writes an explicit own-Manager binding distinct from the empty default", async () => {
  await render(); expect(select().value).toBe("");
  expect([...select().options].map((o) => o.value)).toContain(owner.id);
  await change(owner.id);
  expect(select().value).toBe(owner.id); expect(host.textContent).toContain("Bound");
  expect(writes("/put")[0].body.manager_id).toBe(owner.id);
});
it("unbinds immediately and reads back without a form save", async () => {
  bindings = [binding("channel-telegram", "42", other.id)]; policies["channel-telegram"] = true;
  await render(); expect(host.textContent).toContain("Unbinding silences");
  await change(""); expect(writes("/delete")).toHaveLength(1); expect(select().value).toBe("");
  expect(host.textContent).toContain("None — silent"); expect(writes("channel_chats/list")).toHaveLength(2);
});
it("saves both policy values immediately through its dedicated endpoint", async () => {
  await render(); const toggle = host.querySelector('[role="switch"]') as HTMLElement;
  await click(toggle); expect(toggle.getAttribute("aria-checked")).toBe("true");
  await click(toggle); expect(toggle.getAttribute("aria-checked")).toBe("false");
  expect(writes("/set_policy").map((c) => c.body.bound_chats_only)).toEqual([true, false]);
  expect(calls.some((c) => c.path.includes("channel_configs"))).toBe(false);
});
it("keeps a failed selection confirmed and exposes the typed error for retry", async () => {
  intercept = (path) => path.endsWith("/put") ? refused() : undefined;
  await render(); await change(other.id); expect(select().value).toBe("");
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("conflict: Synthetic refusal");
  expect(select().disabled).toBe(false);
  intercept = () => undefined; await change(other.id); expect(select().value).toBe(other.id);
  expect(host.querySelector('[role="alert"]')).toBeNull();
});
it("keeps a refused policy at its last confirmed value and permits retry", async () => {
  intercept = (path) => path.endsWith("/set_policy") ? refused(503, "unavailable") : undefined;
  await render(); const toggle = host.querySelector('[role="switch"]') as HTMLButtonElement;
  await click(toggle); expect(toggle.getAttribute("aria-checked")).toBe("false"); expect(toggle.disabled).toBe(false);
  expect(writes("channel_chats/list")).toHaveLength(2);
  intercept = () => undefined; await click(toggle); expect(toggle.getAttribute("aria-checked")).toBe("true");
});
it("reads the confirmed policy after a failed binding supersedes its pending readback", async () => {
  catalogs["channel-telegram"] = [chat(), chat("43", "Another chat")];
  await render();
  const stale = { ...snapshot("channel-telegram"), bound_chats_only: false };
  const hold = deferred<Response>(); let held = false;
  intercept = (path) => {
    if (path === "channel_chats/list" && !held) { held = true; return hold.promise; }
    if (path.endsWith("/put")) return refused();
  };
  const toggle = host.querySelector('[role="switch"]') as HTMLElement;
  await click(toggle);
  expect(policies["channel-telegram"]).toBe(true);
  const signal = writes("channel_chats/list").at(-1)?.signal;
  await change(other.id, 1);
  expect(signal?.aborted).toBe(true);
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  expect(select(1).value).toBe("");
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("conflict: Synthetic refusal");
  await act(async () => hold.resolve(ok(stale)));
  expect(toggle.getAttribute("aria-checked")).toBe("true");
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("conflict: Synthetic refusal");
});
it("reads current server state after a refused unbind while keeping its error", async () => {
  bindings = [binding("channel-telegram", "42", other.id)];
  await render();
  intercept = (path) => {
    if (path.endsWith("/delete")) { policies["channel-telegram"] = true; return refused(); }
  };
  await change("");
  expect(select().value).toBe(other.id);
  expect(host.querySelector('[role="switch"]')?.getAttribute("aria-checked")).toBe("true");
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("conflict: Synthetic refusal");
});
it.each([false, true])("labels the unbound option by its effect with Only bound chats %s", async (policy) => {
  policies["channel-telegram"] = policy;
  await render();
  expect(select().querySelector('option[value=""]')?.textContent).toBe(
    policy ? "Leave unbound (silent)" : "Use this bot's Manager (no binding)");
  expect(select().querySelector(`option[value="${owner.id}"]`)?.textContent).toBe(owner.name);
});
it("reloads from fresh page boundaries after a write without losing or duplicating chats", async () => {
  catalogs["channel-telegram"] = Array.from({ length: 39 }, (_, i) => chat(String(i + 10), `Room ${i + 10}`));
  pageResult = (channel, after) => {
    const all = snapshot(channel); const remaining = all.chats.filter((c) => !after || c.chat_id > after);
    const chats = remaining.slice(0, 20); const has_more = remaining.length > chats.length;
    return { ...all, chats, has_more, next_cursor: has_more ? chats.at(-1)!.chat_id : null };
  };
  await render(); await click(button("Load more chats"));
  expect(host.querySelectorAll("select")).toHaveLength(39);
  catalogs["channel-telegram"].unshift(chat("09", "New room"));
  await change(other.id);
  expect([...host.querySelectorAll("label")].map((label) => label.textContent)).toEqual(
    catalogs["channel-telegram"].map((c) => `Bind ${c.name} to`));
  expect(select(1).value).toBe(other.id);
});
it("disables only the pending row while other rows and the policy remain usable", async () => {
  catalogs["channel-telegram"] = [chat(), chat("43", "Another chat")];
  const hold = deferred<Response>(); intercept = (path) => path.endsWith("/put") ? hold.promise : undefined;
  await render(); await change(other.id); expect(select().disabled).toBe(true); expect(select(1).disabled).toBe(false);
  expect((host.querySelector('[role="switch"]') as HTMLButtonElement).disabled).toBe(false);
  await act(async () => hold.resolve(refused())); expect(select().disabled).toBe(false); expect(select().value).toBe("");
});
it("labels disabled targets off and explains their turned-off response after binding", async () => {
  await render(); expect(select().querySelector(`option[value="${off.id}"]`)?.textContent).toContain("(off)");
  await change(off.id); expect(select().value).toBe(off.id); expect(host.textContent).toContain("existing turned-off response");
});
it("shows a deleted bound target unavailable rather than off", async () => {
  const b = binding("channel-telegram", "42", "manager-deleted"); b.manager_deleted = true; b.manager_name = "Removed";
  bindings = [b]; await render(); expect(select().value).toBe(b.manager_id);
  expect(select().selectedOptions[0].textContent).toBe("Removed (unavailable)");
  expect(select().selectedOptions[0].disabled).toBe(true); expect(host.textContent).not.toContain("existing turned-off response");
});
it("refreshes choices on a deleted-between-fetch-and-save 404 without false success", async () => {
  await render(); intercept = (path) => {
    if (!path.endsWith("/put")) return;
    managers = [owner, off]; return refused(404, "not_found");
  };
  await change(other.id); expect(select().value).toBe("");
  expect(select().querySelector(`option[value="${other.id}"]`)).toBeNull();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("not_found");
});
it("unions paginated chats and suppression history with no duplicate selectors", async () => {
  bindings = [binding("channel-telegram", "99", other.id)];
  pageResult = (channel, after) => ({ ...snapshot(channel), chats: after ? [{ ...chat("99", null), count: null, last_at: null, binding: bindings[0] }] : [chat()],
    unbound_chats: [{ chat_id: "42", name: "Sample chat", count: 4, last_at: stamp }, { chat_id: "99", name: null, count: 3, last_at: stamp }],
    has_more: !after, next_cursor: after ? null : "42" });
  await render(); expect(host.querySelectorAll("select")).toHaveLength(2); expect(select(1).value).toBe(other.id);
  expect(select(1).closest("li")?.textContent).toContain("Bound"); expect(select(1).closest("li")?.textContent).not.toContain("Silenced");
  await click(button("Load more chats")); expect(host.querySelectorAll("select")).toHaveLength(2);
  expect(writes("channel_chats/list").at(-1)?.body.after).toBe("42");
  expect(select(1).closest("li")?.textContent).toContain("Seen: Unknown · Last seen: Unknown");
});
it("shows a never-observed explicit binding with unknown metadata and its native id", async () => {
  bindings = [binding("channel-telegram", "42", owner.id)]; catalogs["channel-telegram"] = [{ ...chat("42", null), count: null, last_at: null }];
  await render(); expect(host.textContent).toContain("Seen: Unknown · Last seen: Unknown");
  expect(host.querySelector("label")?.textContent).toBe("Bind 42 to");
});
it("refresh picks up another Settings page's write", async () => {
  await render(); bindings = [binding("channel-telegram", "42", other.id)];
  await click(button("Refresh chats")); expect(select().value).toBe(other.id);
});
it("keeps loading distinct from an empty loaded catalog", async () => {
  const hold = deferred<Response>(); intercept = (path) => path === "channel_chats/list" ? hold.promise : undefined;
  await render(); expect(host.textContent).toContain("Loading chats"); expect(host.textContent).not.toContain("No chats");
  await act(async () => hold.resolve(ok({ ...snapshot("channel-telegram"), chats: [] })));
  expect(host.textContent).toContain("No chats"); expect(host.textContent).not.toContain("Loading chats");
});
it("shows a failed load as error rather than an empty catalog and allows refresh", async () => {
  intercept = (path) => path === "channel_chats/list" ? refused(503, "unavailable") : undefined;
  await render(); expect(host.querySelector('[role="alert"]')?.textContent).toContain("unavailable"); expect(host.textContent).not.toContain("No chats");
  intercept = () => undefined; await click(button("Refresh chats")); expect(select()).toBeDefined();
});
it("aborts identity-scoped reads and suppresses stale completion after switching bots", async () => {
  const hold = deferred<Response>(); intercept = (path, body) => path === "channel_chats/list" && body.channel_id === "channel-telegram" ? hold.promise : undefined;
  await render(); const signal = writes("channel_chats/list")[0].signal;
  await render("channel-telegram-2"); expect(signal?.aborted).toBe(true);
  await act(async () => hold.resolve(ok({ ...snapshot("channel-telegram"), chats: [chat("777", "Stale chat")] })));
  expect(host.textContent).not.toContain("Stale chat"); expect(select().value).toBe("");
});
it("a late write cannot overwrite another bot with the same chat id", async () => {
  const hold = deferred<Response>(); intercept = (path) => path.endsWith("/put") ? hold.promise : undefined;
  await render(); await change(other.id); await render("channel-telegram-2");
  const reads = writes("channel_chats/list").length;
  await act(async () => hold.resolve(ok({ binding: binding("channel-telegram", "42", other.id) })));
  expect(select().value).toBe(""); expect(writes("channel_chats/list")).toHaveLength(reads);
});
it("a stale refresh cannot overwrite a successful binding readback", async () => {
  await render(); const hold = deferred<Response>(); let held = false;
  intercept = (path) => { if (path === "channel_chats/list" && !held) { held = true; return hold.promise; } };
  await click(button("Refresh chats")); const signal = writes("channel_chats/list").at(-1)?.signal;
  await change(other.id); expect(select().value).toBe(other.id); expect(signal?.aborted).toBe(true);
  await act(async () => hold.resolve(ok({ ...snapshot("channel-telegram"), chats: [chat()] })));
  expect(select().value).toBe(other.id);
});
it("reports a successful write whose readback failed without claiming confirmation", async () => {
  await render(); let written = false;
  intercept = (path) => { if (path.endsWith("/put")) written = true; if (written && path === "channel_chats/list") return refused(503, "unavailable"); };
  await change(other.id); expect(select().value).toBe(""); expect(host.querySelector('[role="alert"]')?.textContent).toContain("confirm server state");
  intercept = () => undefined; await click(button("Refresh chats")); expect(select().value).toBe(other.id);
});
