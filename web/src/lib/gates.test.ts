import { describe, expect, it } from "vitest";
import type { GateChannelPosts, UserGate } from "@/lib/api";
import { GATE_STATES, POST_ERROR_SHOWN_CHARS, gateLook, gatePostsText, gateTurn, gateWaitsOn } from "@/lib/gates";

type Gate = Pick<UserGate, "state" | "waiting_on">;
const GATE_TURNS_USER = { label: "Your turn", variant: "warning", tone: GATE_STATES.open.tone };

describe("gateLook", () => {
  const cases: [string, Gate, { label: string; variant: string; tone: string }, Gate["waiting_on"]][] = [
    ["an open gate waits for the user", { state: "open", waiting_on: "user" },
     { label: "Waiting for you", variant: "warning", tone: "text-warning" }, null],
    ["a discussed gate on the user's turn looks like an open one", { state: "discussing", waiting_on: "user" },
     { label: "Your turn", variant: "warning", tone: GATE_STATES.open.tone }, "user"],
    ["a discussed gate on the Planner's turn looks like running work", { state: "discussing", waiting_on: "planner" },
     { label: "Planner answering", variant: "default", tone: "text-primary" }, "planner"],
    ["a passed gate reads by its state", { state: "passed", waiting_on: null },
     { label: "Passed", variant: "success", tone: "text-success" }, null],
  ];

  it.each(cases)("%s", (_, gate, look, turn) => {
    expect(gateLook(gate, true)).toEqual(look);
    expect(gateTurn(gate, true)).toBe(turn);
  });

  it("gives a stopped task's gate on the Planner's turn to the user, as its panel does", () => {
    const gate: Gate = { state: "discussing", waiting_on: "planner" };
    expect(gateWaitsOn(gate, false)).toBe("user");
    expect(gateTurn(gate, false)).toBe("user");
    expect(gateLook(gate, false)).toEqual(GATE_TURNS_USER);
    expect(gateWaitsOn({ waiting_on: "user" }, false)).toBe("user");
    expect(gateWaitsOn({ waiting_on: null }, false)).toBeNull();
    expect(gateLook({ state: "passed", waiting_on: null }, false).label).toBe("Passed");
  });
});

describe("gatePostsText", () => {
  const posts = (extra: Partial<GateChannelPosts> = {}): GateChannelPosts => ({
    channel: "lark", target: "Lark group oc_123", sent: 2, failed: 0, last_error: null, ...extra,
  });

  it("says nothing for a gate whose messages go to no chat", () => {
    expect(gatePostsText(null)).toBeNull();
  });

  it("names the chat, and no failure while every message was sent", () => {
    expect(gatePostsText(posts())).toEqual({ where: "Also in Lark group oc_123", failure: null, error: "" });
  });

  it("counts the messages that could not be sent, with the channel and the latest reason", () => {
    expect(gatePostsText(posts({ failed: 1, last_error: "bot is not in the chat" }))?.failure)
      .toBe("1 message could not be sent to Lark: bot is not in the chat");
    expect(gatePostsText(posts({ channel: "telegram", target: "Telegram chat 42", failed: 3, last_error: null })))
      .toEqual({ where: "Also in Telegram chat 42", failure: "3 messages could not be sent to Telegram.", error: "" });
  });

  it("clips a long reason, keeping the whole text for the tooltip", () => {
    const long = "x".repeat(POST_ERROR_SHOWN_CHARS + 50);
    const text = gatePostsText(posts({ failed: 2, last_error: long }));
    expect(text?.failure).toBe(`2 messages could not be sent to Lark: ${"x".repeat(POST_ERROR_SHOWN_CHARS)}…`);
    expect(text?.error).toBe(long);
  });
});
