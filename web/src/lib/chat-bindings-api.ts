import { callV1, confirmV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

export type SenderPolicy = "listed" | "chat";

export class ChatBindingConfirmation extends V1Error {
  readonly confirmation: V1PendingConfirmation;

  constructor(confirmation: V1PendingConfirmation) {
    super(202, "needs_confirmation", confirmation.impact);
    this.confirmation = confirmation;
  }
}

export interface Binding {
  channel_id: string;
  chat_id: string;
  manager_id: string;
  created_at: string;
  allowed_senders: string[];
  sender_policy: SenderPolicy;
  channel_type: "telegram" | "lark" | "lark_bot";
  bot_name: string;
  chat_name: string | null;
  manager_name: string;
  manager_enabled: boolean;
  manager_deleted: boolean;
  /** Stored group default; null is the system default `chat_only`. */
  default_scope: string | null;
  effective_default_scope: string;
  legacy_allowed: boolean;
  suggested_scope: string | null;
}

export interface CapabilitySet {
  id: string;
  name: string;
  builtin: boolean;
  current_version: number;
  items: unknown[];
}

export interface ScopeOverride {
  principal_id: string;
  principal_kind: "person" | "bot";
  sender_ids: string[];
  scope_id: string;
  created_at: string;
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

export async function putChatBinding(channelId: string, chatId: string, managerId: string, allowedSenders?: string[],
  senderPolicy?: SenderPolicy): Promise<{ binding: Binding }> {
  const result = await callV1<{ binding: Binding }>("/api/v1/manager_chat_bindings/put",
    { channel_id: channelId, chat_id: chatId, manager_id: managerId,
      ...(allowedSenders === undefined ? {} : { allowed_senders: allowedSenders }),
      ...(senderPolicy === undefined ? {} : { sender_policy: senderPolicy }) });
  if ("status" in result) throw new ChatBindingConfirmation(result);
  return result;
}

export async function confirmChatBinding(confirmation: V1PendingConfirmation): Promise<{ binding: Binding }> {
  const result = await confirmV1<{ binding: Binding }>(confirmation.confirmation_id, confirmation.session);
  if ("status" in result) throw new ChatBindingConfirmation(result);
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

function address(binding: Pick<Binding, "channel_id" | "chat_id">) {
  return { channel_id: binding.channel_id, chat_id: binding.chat_id };
}

export async function listCapabilitySets(signal?: AbortSignal): Promise<CapabilitySet[]> {
  const result = await callV1<{ sets: CapabilitySet[] }>("/api/v1/capability_sets/list", {}, { signal });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  return result.sets;
}

/** Keeps the binding's current Manager. Null scope is the system default. */
export async function putDefaultScope(binding: Binding, scope: string | null, stopNow: boolean): Promise<{ binding: Binding }> {
  const result = await callV1<{ binding: Binding }>("/api/v1/manager_chat_bindings/put",
    { ...address(binding), manager_id: binding.manager_id, default_scope: scope, stop_now: stopNow });
  if ("status" in result) throw new ChatBindingConfirmation(result);
  return result;
}

export async function listOverrides(binding: Pick<Binding, "channel_id" | "chat_id">, signal?: AbortSignal): Promise<ScopeOverride[]> {
  const result = await callV1<{ overrides: ScopeOverride[] }>("/api/v1/chat_scope_overrides/list", address(binding), { signal });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  return result.overrides;
}

export async function putOverride(binding: Pick<Binding, "channel_id" | "chat_id">, body: {
  principal_kind: "person" | "bot"; sender_ids: string[]; scope_id: string; stop_now: boolean;
}): Promise<ScopeOverride> {
  const result = await callV1<ScopeOverride>("/api/v1/chat_scope_overrides/put", { ...address(binding), ...body });
  if ("status" in result) throw new ChatBindingConfirmation(result);
  return result;
}

export async function deleteOverride(binding: Pick<Binding, "channel_id" | "chat_id">, principalId: string,
  stopNow: boolean): Promise<{ deleted: boolean }> {
  const result = await callV1<{ deleted: boolean }>("/api/v1/chat_scope_overrides/delete",
    { ...address(binding), principal_id: principalId, stop_now: stopNow });
  if ("status" in result) throw new ChatBindingConfirmation(result);
  return result;
}
