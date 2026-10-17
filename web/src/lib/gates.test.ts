import { describe, expect, it } from "vitest";
import type { UserGate } from "@/lib/api";
import { GATE_STATES, gateLook, gateTurn } from "@/lib/gates";

type Gate = Pick<UserGate, "state" | "waiting_on">;

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
    expect(gateLook(gate)).toEqual(look);
    expect(gateTurn(gate)).toBe(turn);
  });
});
