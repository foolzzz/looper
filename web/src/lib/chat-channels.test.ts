import { describe, expect, it } from "vitest";

import { CHAT_CHANNELS, channelLabel, larkPeopleSearchNote, undeliveredText } from "@/lib/chat-channels";

describe("channelLabel", () => {
  it("lists Lark's bot-token channel after Lark, with its own label", () => {
    expect(channelLabel("lark_bot")).toBe("Lark (bot token)");
    const larkIndex = CHAT_CHANNELS.findIndex((entry) => entry.channel === "lark");
    expect(CHAT_CHANNELS[larkIndex + 1]?.channel).toBe("lark_bot");
  });
});

describe("larkPeopleSearchNote", () => {
  it.each([
    ["lark", "Searches as the user lark-cli is signed in as; the open_ids found are this app's."],
    ["lark_bot", "Searches the app's visible directory by name or email (an email is exact); the open_ids found are this app's."],
  ])("explains the %s search scope and warns about an incomplete directory", (channel, note) => {
    expect(larkPeopleSearchNote(channel, true)).toBe(note);
    expect(larkPeopleSearchNote(channel, false)).toBe(`${note} Only part of the directory was searched: use the person's email.`);
  });
});

describe("undeliveredText", () => {
  it.each([
    ["lark", null, null, null],
    ["lark", undefined, undefined, null],
    ["telegram", "no_chat", null, "Not delivered: Telegram no longer knew this chat (the service restarted meanwhile)."],
    ["lark", "send_failed", "lark-cli exited 2: the bot is not in the chat",
      "Not delivered: Lark could not send it (lark-cli exited 2: the bot is not in the chat)."],
    ["telegram", "send_failed", null, "Not delivered: Telegram could not send it."],
    ["lark_bot", "send_failed", null, "Not delivered: Lark (bot token) could not send it."],
  ] as const)("%s %s %s", (channel, failure, error, expected) => {
    expect(undeliveredText(channel, failure, error)).toBe(expected);
  });
});
