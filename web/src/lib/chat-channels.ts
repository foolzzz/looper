import type { DeliveryFailure } from "@/lib/api";

/** The channels the Manager talks on, in the order the Chat page lists their conversations. */
export const CHAT_CHANNELS: { channel: string; label: string }[] = [
  { channel: "web", label: "Dashboard chat" },
  { channel: "lark", label: "Lark" },
  { channel: "lark_bot", label: "Lark (bot token)" },
  { channel: "telegram", label: "Telegram" },
];

export function channelLabel(channel: string): string {
  return CHAT_CHANNELS.find((c) => c.channel === channel)?.label ?? channel;
}

/** How the Lark sender search works, including whether its directory results are incomplete. */
export function larkPeopleSearchNote(channelType: string, complete: boolean): string {
  const note = channelType === "lark_bot"
    ? "Searches the app's visible directory by name or email (an email is exact); the open_ids found are this app's."
    : "Searches as the user lark-cli is signed in as; the open_ids found are this app's.";
  return complete ? note : `${note} Only part of the directory was searched: use the person's email.`;
}

/** What the Chat page says under a Lark or Telegram answer its channel did not deliver; null for one it sent. */
export function undeliveredText(
  channel: string,
  failure: DeliveryFailure | null | undefined,
  error?: string | null,
): string | null {
  if (!failure) return null;
  const where = channelLabel(channel);
  if (failure === "no_chat") return `Not delivered: ${where} no longer knew this chat (the service restarted meanwhile).`;
  return `Not delivered: ${where} could not send it${error ? ` (${error})` : ""}.`;
}
