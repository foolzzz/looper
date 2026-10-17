import { describe, expect, it } from "vitest";
import { badTaskLimit, changedTaskLimits, createTaskLimits, taskLimitsDraft, taskLimitsOf } from "@/lib/schedules";

// The form's text, as the API takes it.
const typed = (worker: string, reviewer: string) => taskLimitsOf({ worker, reviewer });

describe("createTaskLimits", () => {
  it("sends nothing for a schedule without limits, so its tasks use the default", () => {
    expect(createTaskLimits(typed("", " "))).toEqual({});
  });

  it("sends both roles once one is set, the other as null", () => {
    expect(createTaskLimits(typed(" 4 ", ""))).toEqual({ task_limits: { worker: 4, reviewer: null } });
  });
});

describe("changedTaskLimits", () => {
  const saved = { worker: 4, reviewer: null };

  it("sends nothing while the limits are as saved", () => {
    expect(changedTaskLimits(taskLimitsOf(taskLimitsDraft(saved)), saved)).toEqual({});
  });

  it.each([
    ["a changed limit", typed("6", ""), { worker: 6, reviewer: null }],
    ["a limit set", typed("4", "2"), { worker: 4, reviewer: 2 }],
    ["a cleared limit, as null", typed("", ""), { worker: null, reviewer: null }],
  ])("sends the whole object for %s", (_, limits, expected) => {
    expect(changedTaskLimits(limits, saved)).toEqual({ task_limits: expected });
  });
});

describe("badTaskLimit", () => {
  it.each([
    ["", false],
    ["1", false],
    ["64", false],
    ["0", true],
    ["65", true],
    ["2.5", true],
    ["four", true],
  ])("judges %j against 1..64: bad %s", (text, bad) => {
    expect(badTaskLimit(typed(text, "").worker, 64)).toBe(bad);
  });
});
