import { afterEach, describe, expect, it, vi } from "vitest";
import {
  FIT_PADDING,
  conditionNodes,
  dependencyEdges,
  conditionState,
  fitPadding,
  shownSteps,
  stepColumns,
  stepLevels,
  storeHideSkipped,
  storedHideSkipped,
} from "@/lib/pipeline-layout";
import type { Condition, TaskStep, TaskWait, UserGate } from "@/lib/api";

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

const dependencyPlan = (ready_when: TaskStep["ready_when"]): TaskStep[] => [
  step("dev", 0, "verified"), step("review_a", 1, "pending"),
  { ...step("verify_staging", 2, "pending", ["dev", "review_a"]), ready_when },
];

describe("dependency conditions", () => {
  it("preserves the legacy nodes, edges and positions for plain all_of", () => {
    const legacy = dependencyPlan(null);
    const plain = dependencyPlan({ all_of: ["dev", { all_of: ["review_a"] }] });
    expect(conditionNodes(plain)).toEqual([]);
    expect(dependencyEdges(plain)).toEqual([
      { source: "planner", target: "dev" }, { source: "planner", target: "review_a" },
      { source: "dev", target: "verify_staging" }, { source: "review_a", target: "verify_staging" },
    ]);
    expect(layout(plain)).toEqual(layout(legacy));
    expect(layout(plain)).toEqual({ dev: [0, -X / 2], review_a: [0, X / 2], verify_staging: [1, 0] });
  });

  it("places an any_of junction between its children and the target", () => {
    const plan = dependencyPlan({ any_of: ["dev", { step: "review_a", on: "passed" }] });
    expect(conditionNodes(plan).map((n) => [n.id, n.label])).toEqual([["verify_staging:c0", "任一"]]);
    expect(Object.fromEntries(stepLevels(plan))).toEqual({ dev: 0, review_a: 0, "verify_staging:c0": 1, verify_staging: 2 });
    expect(stepColumns(plan, stepLevels(plan), X).get("verify_staging:c0")).toBe(0);
    expect(dependencyEdges(plan).slice(2)).toEqual([
      { source: "verify_staging:c0", target: "verify_staging" },
      { source: "dev", target: "verify_staging:c0" },
      { source: "review_a", target: "verify_staging:c0", on: "passed" },
    ]);
  });

  it("routes nested groups through distinct junctions in dependency order", () => {
    const plan = dependencyPlan({ any_of: [{ all_of: ["dev", { step: "review_a", on: "completed" }] }, "review_b"] });
    plan.splice(2, 0, step("review_b", 2, "pending"));
    expect(conditionNodes(plan).map((n) => [n.id, n.label])).toEqual([
      ["verify_staging:c0.0", "全部"], ["verify_staging:c0", "任一"],
    ]);
    const levels = stepLevels(plan);
    expect([levels.get("verify_staging:c0.0"), levels.get("verify_staging:c0"), levels.get("verify_staging")]).toEqual([1, 2, 3]);
    expect(dependencyEdges(plan)).toContainEqual({ source: "verify_staging:c0.0", target: "verify_staging:c0" });
  });

  it("keeps explicit single-leaf requirements visible", () => {
    const plan = dependencyPlan({ step: "dev", on: "completed" });
    expect(conditionNodes(plan).map((n) => n.label)).toEqual(["全部"]);
    expect(dependencyEdges(plan)).toContainEqual({ source: "dev", target: "verify_staging:c0", on: "completed" });
  });

  it("does not turn a hidden dropped dependency into a new root step", () => {
    const plan = dependencyPlan({ any_of: ["dev", "review_a"] });
    plan[1].status = "skipped";
    const visible = shownSteps(plan, true).steps;
    expect(dependencyEdges(visible)).not.toContainEqual({ source: "review_a", target: "verify_staging:c0" });
    expect(stepLevels(visible).get("verify_staging")).toBe(2);
  });

  it.each([
    ["verified", null, "passed", "satisfied"],
    ["completed", null, "completed", "satisfied"],
    ["completed", null, "verified", "pending"],
    ["deciding", { status: "completed" }, "completed", "satisfied"],
    ["deciding", { status: "failed" }, "completed", "pending"],
    ["failed", null, "completed", "unsatisfiable"],
    ["skipped", null, "verified", "unsatisfiable"],
  ] as const)("reads %s outcomes for %s / %s", (status, result, on, state) => {
    expect(conditionState({ step: "dev", on }, [{ ...step("dev", 0, status), result }], [], [])).toBe(state);
  });

  it.each([
    ["passed", "passed", "satisfied"],
    ["cancelled", "passed", "unsatisfiable"],
    ["cancelled", "verified", "satisfied"],
    ["open", "completed", "pending"],
  ] as const)("reads gate %s for %s", (state, on, expected) => {
    const gate = { step_id: "dev", state } as UserGate;
    expect(conditionState({ step: "dev", on }, [step("dev", 0, "verified")], [gate], [])).toBe(expected);
  });

  it.each([
    ["done", "ready", "passed", "satisfied"],
    ["done", "resumed", "passed", "satisfied"],
    ["done", "proceeded", "passed", "unsatisfiable"],
    ["done", "cancelled", "completed", "satisfied"],
    ["waiting", null, "completed", "pending"],
  ] as const)("reads wait %s / %s for %s", (state, outcome, on, expected) => {
    const wait = { step_id: "dev", state, outcome } as TaskWait;
    expect(conditionState({ step: "dev", on }, [step("dev", 0, "verified")], [], [wait])).toBe(expected);
  });

  it("shows a missing dependency as impossible", () => {
    expect(conditionState("dev", [], [], [])).toBe("unsatisfiable");
  });

  it.each([
    [{ k_of: 1, of: ["dev", "review_a"] }, "satisfied"],
    [{ k_of: 2, of: ["dev", "review_a"] }, "pending"],
    [{ k_of: 3, of: ["dev", "review_a", "review_b"] }, "unsatisfiable"],
  ] satisfies [Condition, string][])("evaluates threshold boundaries", (condition, state) => {
    const steps = [step("dev", 0, "verified"), step("review_a", 1, "pending"), step("review_b", 2, "failed")];
    expect(conditionState(condition, steps, [], [])).toBe(state);
  });
});

describe("review panel acceptance", () => {
  const panel = (condition: Condition): TaskStep[] => [
    { ...step("dev", 0, "verified"), accept_when: condition },
    ...["review_a", "review_b", "review_c"].map((id, index): TaskStep => ({
      ...step(id, index + 1, "verified", ["dev"]), kind: "review", reviews: "dev",
      ready_when: { step: "dev", on: "completed" }, result: { passed: index < 2 },
    })),
    step("verify_staging", 4, "pending", ["dev"]),
  ];

  it("routes reviews and subsequent work through the acceptance junction", () => {
    const plan = panel({ all_of: [{ step: "review_a", on: "passed" }, { step: "review_b", on: "passed" }] });
    expect(conditionNodes(plan).map((node) => [node.id, node.label])).toEqual([["dev:a0", "必选全部通过"]]);
    expect(Object.fromEntries(stepLevels(plan))).toEqual({ dev: 0, review_a: 1, review_b: 1, review_c: 1,
      "dev:a0": 2, verify_staging: 3 });
    expect(dependencyEdges(plan)).toContainEqual({ source: "dev:a0", target: "verify_staging" });
    expect(dependencyEdges(plan)).toContainEqual({ source: "review_c", target: "dev:a0" });
  });

  it("renders threshold acceptance without waiting for the third conclusion", () => {
    const condition: Condition = { k_of: 2, of: ["review_a", "review_b", "review_c"].map((step) => ({ step, on: "passed" })) };
    const plan = panel(condition);
    plan[3].status = "dispatched";
    expect(conditionNodes(plan)[0].label).toBe("2/3 通过");
    expect(conditionState(condition, plan, [], [])).toBe("satisfied");
  });

  it.each([
    [{ passed: false }, "unsatisfiable"],
    [{ passed: false, status: "passed_by_adjudication" }, "satisfied"],
  ])("uses review conclusions rather than the terminal step state", (result, expected) => {
    const plan = panel("review_c");
    plan[3].result = result;
    expect(conditionState({ step: "review_c", on: "passed" }, plan, [], [])).toBe(expected);
    expect(conditionState({ step: "review_c", on: "completed" }, plan, [], [])).toBe("satisfied");
  });
});
