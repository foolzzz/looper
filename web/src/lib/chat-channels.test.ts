import { describe, expect, it } from "vitest";

import { undeliveredText } from "@/lib/chat-channels";

describe("undeliveredText", () => {
  it.each([
    ["lark", null, null, null],
    ["lark", undefined, undefined, null],
    ["telegram", "no_chat", null, "Not delivered: Telegram no longer knew this chat (the service restarted meanwhile)."],
    ["lark", "send_failed", "lark-cli exited 2: the bot is not in the chat",
      "Not delivered: Lark could not send it (lark-cli exited 2: the bot is not in the chat)."],
    ["telegram", "send_failed", null, "Not delivered: Telegram could not send it."],
  ] as const)("%s %s %s", (channel, failure, error, expected) => {
    expect(undeliveredText(channel, failure, error)).toBe(expected);
  });
});
