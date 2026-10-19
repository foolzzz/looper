import { describe, expect, it } from "vitest";
import { runTimeoutText } from "@/lib/schedule-text";

describe("runTimeoutText", () => {
  it.each([
    ["default", null, "The default (no limit)"],
    ["default", 5400, "The default (90 min)"],
    [null, null, "None"],
    [1200, 1200, "20 min"],
  ] as const)("%s (effective %s) reads %s", (own, effective, text) => {
    expect(runTimeoutText({ run_timeout_seconds: own, run_timeout_effective_seconds: effective })).toBe(text);
  });
});
