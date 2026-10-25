import { callV1, V1Error } from "@/lib/v1";

export interface Binding {
  channel_id: string;
  chat_id: string;
  manager_id: string;
  created_at: string;
  channel_type: "telegram" | "lark" | "lark_bot";
  bot_name: string;
  chat_name: string | null;
  manager_name: string;
  manager_enabled: boolean;
  manager_deleted: boolean;
}

export interface UnboundChat {
  chat_id: string;
  name: string | null;
  count: number;
  last_at: string;
}

export interface SeenChat {
  chat_id: string;
  name: string | null;
  count: number | null;
  last_at: string | null;
  binding: Binding | null;
  effective_manager_id: string;
  effective_manager_name: string;
  effective_manager_enabled: boolean;
  effective_manager_deleted: boolean;
}

export interface ChannelChats {
  channel_id: string;
  bot_manager_id: string;
  bound_chats_only: boolean;
  chats: SeenChat[];
  unbound_chats: UnboundChat[];
  next_cursor: string | null;
  has_more: boolean;
}

export async function fetchChatBindings(filters: { channel_id?: string; manager_id?: string } = {},
  signal?: AbortSignal): Promise<Binding[]> {
  const result = await callV1<{ bindings: Binding[] }>("/api/v1/manager_chat_bindings/list", filters, { signal });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  return result.bindings;
}

export async function putChatBinding(channelId: string, chatId: string, managerId: string): Promise<{ binding: Binding }> {
  const result = await callV1<{ binding: Binding }>("/api/v1/manager_chat_bindings/put",
    { channel_id: channelId, chat_id: chatId, manager_id: managerId });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  return result;
}

export async function deleteChatBinding(channelId: string, chatId: string): Promise<{
  channel_id: string; chat_id: string; deleted: boolean;
}> {
  const result = await callV1<{ channel_id: string; chat_id: string; deleted: boolean }>(
    "/api/v1/manager_chat_bindings/delete", { channel_id: channelId, chat_id: chatId });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  return result;
}

export async function fetchChannelChats(channelId: string, options: { limit?: number; after?: string } = {},
  signal?: AbortSignal): Promise<ChannelChats> {
  const result = await callV1<ChannelChats>("/api/v1/channel_chats/list", { channel_id: channelId, ...options }, { signal });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  return result;
}

export async function setBoundChatsOnly(channelId: string, boundChatsOnly: boolean): Promise<{
  channel_id: string; bound_chats_only: boolean;
}> {
  const result = await callV1<{ channel_id: string; bound_chats_only: boolean }>("/api/v1/channel_chats/set_policy",
    { channel_id: channelId, bound_chats_only: boundChatsOnly });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  return result;
}
