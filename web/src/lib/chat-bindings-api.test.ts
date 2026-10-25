import { afterEach, expect, it, vi } from "vitest";
import { deleteChatBinding, fetchChannelChats, fetchChatBindings, putChatBinding, setBoundChatsOnly,
  type Binding, type ChannelChats } from "./chat-bindings-api";
import { V1Error } from "@/lib/v1";

afterEach(() => vi.unstubAllGlobals());

const binding: Binding = {
  channel_id: "bot-example", chat_id: "-100123", manager_id: "manager-review",
  created_at: "2026-10-25T10:00:00+00:00", channel_type: "telegram", bot_name: "Example",
  chat_name: null, manager_name: "Review", manager_enabled: false, manager_deleted: true,
};
const catalog: ChannelChats = {
  channel_id: binding.channel_id, bot_manager_id: "manager-owner", bound_chats_only: false,
  chats: [{ chat_id: binding.chat_id, name: null, count: null, last_at: null, binding,
    effective_manager_id: binding.manager_id, effective_manager_name: binding.manager_name,
    effective_manager_enabled: false, effective_manager_deleted: true }],
  unbound_chats: [{ chat_id: "-100456", name: null, count: 1, last_at: binding.created_at }],
  next_cursor: null, has_more: false,
};

it("maps all five operations to POST bodies and preserves false and null results", async () => {
  const calls: unknown[] = [];
  const deleted = { channel_id: binding.channel_id, chat_id: binding.chat_id, deleted: false };
  const policy = { channel_id: binding.channel_id, bound_chats_only: false };
  const answers = [{ bindings: [binding] }, { binding }, deleted, catalog, policy];
  vi.stubGlobal("fetch", vi.fn(async (url, init) => {
    calls.push([url, init.method, JSON.parse(init.body)]);
    return Response.json({ status: "ok", result: answers.shift() });
  }));
  await expect(fetchChatBindings({ channel_id: binding.channel_id, manager_id: binding.manager_id })).resolves.toEqual([binding]);
  await expect(putChatBinding(binding.channel_id, binding.chat_id, binding.manager_id)).resolves.toEqual({ binding });
  await expect(deleteChatBinding(binding.channel_id, binding.chat_id)).resolves.toEqual(deleted);
  await expect(fetchChannelChats(binding.channel_id, { limit: 20, after: "-100000" })).resolves.toEqual(catalog);
  await expect(setBoundChatsOnly(binding.channel_id, false)).resolves.toEqual(policy);
  expect(calls).toEqual([
    ["/api/v1/manager_chat_bindings/list", "POST", { channel_id: binding.channel_id, manager_id: binding.manager_id }],
    ["/api/v1/manager_chat_bindings/put", "POST", { channel_id: binding.channel_id, chat_id: binding.chat_id, manager_id: binding.manager_id }],
    ["/api/v1/manager_chat_bindings/delete", "POST", { channel_id: binding.channel_id, chat_id: binding.chat_id }],
    ["/api/v1/channel_chats/list", "POST", { channel_id: binding.channel_id, limit: 20, after: "-100000" }],
    ["/api/v1/channel_chats/set_policy", "POST", policy],
  ]);
});

it("omits optional filters and pagination fields", async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ status: "ok", result: { bindings: [] } }))
    .mockResolvedValueOnce(Response.json({ status: "ok", result: catalog }));
  vi.stubGlobal("fetch", fetcher);
  await expect(fetchChatBindings()).resolves.toEqual([]);
  await fetchChannelChats(binding.channel_id);
  expect(fetcher.mock.calls.map(([, init]) => JSON.parse(init.body))).toEqual([{}, { channel_id: binding.channel_id }]);
});

it.each(["bindings", "chats"])("passes the abort signal for %s reads and preserves cancellation", async (kind) => {
  const controller = new AbortController();
  const aborted = new DOMException("Cancelled", "AbortError");
  const fetcher = vi.fn(async (_url, init) => {
    expect(init.signal).toBe(controller.signal);
    throw aborted;
  });
  vi.stubGlobal("fetch", fetcher);
  controller.abort();
  const pending = kind === "bindings" ? fetchChatBindings({}, controller.signal)
    : fetchChannelChats(binding.channel_id, {}, controller.signal);
  await expect(pending).rejects.toBe(aborted);
});

it("preserves structured operation failures", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "error",
    error: { code: "forbidden", message: "Operator access required" } }, { status: 403 })));
  await expect(setBoundChatsOnly(binding.channel_id, true)).rejects.toEqual(
    new V1Error(403, "forbidden", "Operator access required"));
});

it.each([
  () => fetchChatBindings(),
  () => putChatBinding(binding.channel_id, binding.chat_id, binding.manager_id),
  () => deleteChatBinding(binding.channel_id, binding.chat_id),
  () => fetchChannelChats(binding.channel_id),
  () => setBoundChatsOnly(binding.channel_id, true),
])("rejects unexpected pending responses without confirming them for %s", async (call) => {
  const fetcher = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending-example",
    impact: "Operator access required", expires_in_seconds: 60 }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  await expect(call()).rejects.toMatchObject({ name: "V1Error", status: 202, code: "needs_confirmation" });
  expect(fetcher).toHaveBeenCalledTimes(1);
});
