import { describe, expect, it } from "vitest";
import type { UserGate } from "@/lib/api";
import { GATE_STATES, gateLook, gateTurn, gateWaitsOn } from "@/lib/gates";

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
