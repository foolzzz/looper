/** The channels the Manager talks on, in the order the Chat page lists their conversations. */
export const CHAT_CHANNELS: { channel: string; label: string }[] = [
  { channel: "web", label: "Dashboard chat" },
  { channel: "lark", label: "Lark" },
  { channel: "telegram", label: "Telegram" },
];

export function channelLabel(channel: string): string {
  return CHAT_CHANNELS.find((c) => c.channel === channel)?.label ?? channel;
}
