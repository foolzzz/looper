import { afterEach, describe, expect, it, vi } from "vitest";
import {
  FIT_PADDING,
  fitPadding,
  shownSteps,
  stepColumns,
  stepLevels,
  storeHideSkipped,
  storedHideSkipped,
} from "@/lib/pipeline-layout";
import type { TaskStep } from "@/lib/api";

const step = (id: string, index: number, status: string, depends_on: string[] = []) =>
  ({ id, step_index: index, step_key: id, status, depends_on }) as TaskStep;

// s1, then s2 and s3 beside each other; a replan dropped s3 and the s4 after it, and added s5 after s2.
const PLAN = [
  step("s1", 0, "verified"),
  step("s2", 1, "verified", ["s1"]),
  step("s3", 2, "skipped", ["s1"]),
  step("s4", 3, "skipped", ["s3"]),
  step("s5", 4, "pending", ["s2"]),
];
const X = 216;

function layout(steps: TaskStep[]) {
  const levels = stepLevels(steps);
  const x = stepColumns(steps, levels, X);
  return Object.fromEntries(steps.map((s) => [s.id, [levels.get(s.id), x.get(s.id)]]));
}

describe("shownSteps", () => {
  it("leaves out the dropped steps and counts them, or shows every step", () => {
    expect(shownSteps(PLAN, true)).toEqual({ steps: [PLAN[0], PLAN[1], PLAN[4]], hidden: 2 });
    expect(shownSteps(PLAN, false)).toEqual({ steps: PLAN, hidden: 0 });
  });
});

describe("the layout", () => {
  it("puts a dropped step on the right of its row, and its own row under it", () => {
    expect(layout(PLAN)).toEqual({ s1: [0, 0], s2: [1, 0], s3: [1, X], s4: [2, X], s5: [2, 0] });
  });

  it("leaves no gap where hidden steps were: rows and places are worked out without them", () => {
    const shown = layout(shownSteps(PLAN, true).steps);
    expect(shown).toEqual({ s1: [0, 0], s2: [1, 0], s5: [2, 0] });
    // A row of dropped steps alone goes with them.
    const dropped = [step("s1", 0, "verified"), step("s2", 1, "skipped", ["s1"]), step("s3", 2, "skipped", ["s2"])];
    expect(Math.max(...stepLevels(shownSteps(dropped, true).steps).values())).toBe(0);
    expect(Math.max(...stepLevels(dropped).values())).toBe(2);
    // A step drawn after a hidden one only takes its row from the steps that are drawn.
    const after = [...PLAN, step("s6", 5, "pending", ["s4"])];
    expect(layout(after).s6[0]).toBe(3);
    expect(layout(shownSteps(after, true).steps).s6[0]).toBe(0);
  });

  it("centres a row's live steps around the steps they depend on", () => {
    const wide = [step("s1", 0, "verified"), step("a", 1, "skipped", ["s1"]), step("b", 2, "pending", ["s1"]),
                  step("c", 3, "pending", ["s1"])];
    expect(layout(shownSteps(wide, true).steps)).toEqual({ s1: [0, 0], b: [1, -X / 2], c: [1, X / 2] });
  });
});

describe("the viewer's choice", () => {
  afterEach(() => vi.unstubAllGlobals());

  const storage = (stored: Record<string, string> = {}) => {
    vi.stubGlobal("window", { localStorage: {
      getItem: (key: string) => stored[key] ?? null,
      setItem: (key: string, value: string) => void (stored[key] = value),
    } });
    return stored;
  };

  it("hides the dropped steps when the viewer never chose, and keeps what they chose", () => {
    const stored = storage();
    expect(storedHideSkipped()).toBe(true);
    storeHideSkipped(false);
    expect(Object.values(stored)).toEqual(["false"]);
    expect(storedHideSkipped()).toBe(false);
    storeHideSkipped(true);
    expect(storedHideSkipped()).toBe(true);
  });

  it("hides them, and does not throw, without storage", () => {
    vi.stubGlobal("window", { get localStorage(): Storage { throw new DOMException("denied", "SecurityError"); } });
    expect(storedHideSkipped()).toBe(true);
    expect(() => storeHideSkipped(false)).not.toThrow();
  });
});

describe("fitPadding", () => {
  const frame = { left: 0, top: 257, width: 390, height: 450 };
  const controls = { left: 15, top: 257 + 450 - 15 - 82, width: 28, height: 82 }; // the zoom controls, bottom left

  it("leaves the zoom controls' room on the left and the toolbar's at the bottom by default", () => {
    expect(FIT_PADDING).toEqual({ top: 0.12, right: 0.12, left: "56px", bottom: "80px" });
    expect(fitPadding(frame, [])).toEqual(FIT_PADDING);
    expect(fitPadding(frame, [controls, { left: 67, top: 257 + 450 - 15 - 54, width: 308, height: 54 }])).toEqual(FIT_PADDING);
  });

  it("leaves more beside wider zoom controls, and at the bottom when the toolbar wraps", () => {
    expect(fitPadding(frame, [{ ...controls, width: 44 }])).toMatchObject({ left: "67px", bottom: "80px" });
    expect(fitPadding(frame, [{ left: 67, top: 257 + 450 - 15 - 100, width: 308, height: 100 }]))
      .toMatchObject({ left: "56px", bottom: "123px" });
  });

  it("puts a column on the right on the right", () => {
    expect(fitPadding(frame, [{ left: 390 - 15 - 28, top: 400, width: 28, height: 82 }])).toMatchObject({ right: "51px", left: "56px" });
  });

  it("leaves room under full screen's way out at the top", () => {
    expect(fitPadding({ left: 0, top: 0, width: 390, height: 844 }, [{ left: 244, top: 15, width: 131, height: 52 },
                                                                     { left: 67, top: 765, width: 308, height: 64 }]))
      .toEqual({ top: "75px", right: 0.12, left: "56px", bottom: "87px" });
  });
});
