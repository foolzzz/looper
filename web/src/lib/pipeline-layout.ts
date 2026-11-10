/** Where the Pipeline tab draws a plan's steps: a row per depth in the dependency graph, and a place in it. */
import type { FitViewOptions } from "@xyflow/react";
import type { Condition, ConditionState, TaskStep, TaskWait, UserGate } from "@/lib/api";

type Padding = Exclude<NonNullable<FitViewOptions["padding"]>, number | string>;

/** The room a fit leaves around the graph: 12% at the top and on the right; on the left, the zoom controls' room
 * (26 px wide, a 15 px margin and a gap); at the bottom, the toolbar's (54 px on a touch screen, its 15 px margin
 * and a gap): so neither covers a node. */
const CONTROLS_ROOM = 56;
const TOOLBAR_ROOM = 80;
export const FIT_PADDING: Padding = { top: 0.12, right: 0.12, left: `${CONTROLS_ROOM}px`, bottom: `${TOOLBAR_ROOM}px` };

/** The room a fit leaves, with the graph's panels measured (one frame, the window's coordinates), wherever that is
 * more than `FIT_PADDING`'s, with a gap: beside a panel taller than it is wide (the zoom controls, a column at a
 * side), over one in the lower half (the toolbar), under one in the upper half (full screen's way out). */
export function fitPadding(
  frame: { left: number; top: number; width: number; height: number },
  panels: { left: number; top: number; width: number; height: number }[],
): Padding {
  const gap = 8;
  let [top, right, bottom, left] = [0, 0, TOOLBAR_ROOM, CONTROLS_ROOM];
  for (const panel of panels) {
    if (panel.height > panel.width) {
      if (panel.left + panel.width / 2 < frame.left + frame.width / 2) left = Math.max(left, panel.left + panel.width - frame.left + gap);
      else right = Math.max(right, frame.left + frame.width - panel.left + gap);
    } else if (panel.top - frame.top > frame.height / 2) bottom = Math.max(bottom, frame.top + frame.height - panel.top + gap);
    else top = Math.max(top, panel.top + panel.height - frame.top + gap);
  }
  const px = (value: number) => `${Math.ceil(value)}px` as const;
  return { top: top ? px(top) : FIT_PADDING.top, right: right ? px(right) : FIT_PADDING.right, bottom: px(bottom), left: px(left) };
}

/** The steps the Pipeline draws: every one, or all but the ones a replan dropped (skipped); `hidden` counts those
 * left out. */
export function shownSteps(steps: TaskStep[], hideSkipped: boolean): { steps: TaskStep[]; hidden: number } {
  if (!hideSkipped) return { steps, hidden: 0 };
  const shown = steps.filter((s) => s.status !== "skipped");
  return { steps: shown, hidden: steps.length - shown.length };
}

/** Shorthand AND dependencies keep the original graph and details cards. */
export function isPlainAllOf(condition: Condition): boolean {
  return typeof condition === "string" || "all_of" in condition && condition.all_of.every(isPlainAllOf);
}

type Dependency = { source: string; target: string; on?: "completed" | "verified" | "passed" };
type LayoutItem = Pick<TaskStep, "id" | "step_index" | "status" | "depends_on">;
export interface ConditionNode extends LayoutItem {
  stepId: string;
  condition: Condition;
  label: string;
  inputs: Dependency[];
  acceptance?: boolean;
}

/** Post-order junctions: path 0 is the root, 0.1 its second child group. */
export function conditionNodes(steps: TaskStep[]): ConditionNode[] {
  const nodes: ConditionNode[] = [];
  for (const step of steps) {
    for (const [condition, prefix, acceptance] of [
      [step.kind === "review" ? null : step.ready_when, "c", false], [step.accept_when, "a", true],
    ] as const) {
      if (condition == null || !acceptance && isPlainAllOf(condition)) continue;
      const visit = (condition: Condition, path: string): string => {
        const id = `${step.id}:${prefix}${path}`;
        const children = typeof condition === "string" || "step" in condition ? [condition]
          : "all_of" in condition ? condition.all_of : "any_of" in condition ? condition.any_of : condition.of;
        const label = typeof condition === "string" || "step" in condition || "all_of" in condition ? "全部"
          : "any_of" in condition ? "任一" : `${condition.k_of}/${children.length}`;
        const inputs = children.map((child, index): Dependency => {
          if (typeof child === "string") return { source: child, target: id };
          if ("step" in child) return { source: child.step, target: id, on: child.on };
          return { source: visit(child, `${path}.${index}`), target: id };
        });
        nodes.push({ id, stepId: step.id, condition, label: acceptance && path === "0"
          ? label === "全部" ? "必选全部通过" : `${label} 通过` : label, inputs, acceptance,
          depends_on: inputs.map((input) => input.source), step_index: step.step_index, status: step.status });
        return id;
      };
      visit(condition, "0");
    }
  }
  return nodes;
}

function layoutItems(steps: TaskStep[]): LayoutItem[] {
  const nodes = conditionNodes(steps);
  const roots = new Map(nodes.filter((node) => !node.acceptance).map((node) => [node.stepId, node.id]));
  const acceptance = new Map(nodes.filter((node) => node.id.endsWith(":a0")).map((node) => [node.stepId, node.id]));
  const source = (id: string) => acceptance.get(id) ?? id;
  return [...steps.map((step) => ({ ...step, depends_on: roots.has(step.id) ? [roots.get(step.id)!]
    : step.depends_on.map((id) => step.reviews === id ? id : source(id)) })),
    ...nodes.map((node) => {
      const inputs = node.inputs.map((input) => ({ ...input,
        source: input.on === "completed" ? input.source : source(input.source) }));
      return { ...node, depends_on: inputs.map((input) => input.source), inputs };
    })];
}

/** The same edges drive layering and drawing, omitting only dependencies hidden by the viewer. */
export function dependencyEdges(steps: TaskStep[]): Dependency[] {
  const items = layoutItems(steps);
  const ids = new Set(items.map((item) => item.id));
  const edges: Dependency[] = items.flatMap((item) => {
    const inputs: Dependency[] = "inputs" in item ? (item as ConditionNode).inputs
      : item.depends_on.map((source) => ({ source, target: item.id,
        ...("kind" in item && item.kind === "review" ? { on: "completed" as const } : {}) }));
    const shown = inputs.filter((input) => ids.has(input.source));
    return shown.length ? shown : [{ source: "planner", target: item.id }];
  });
  for (const step of steps.filter((step) => step.kind === "review" && step.reviews)) {
    const target = `${step.reviews}:a0`;
    if (ids.has(target) && !edges.some((edge) => edge.source === step.id)) edges.push({ source: step.id, target });
  }
  return edges;
}

/** Mirror the API's outcome rules for nested groups (the API reports only the root state). */
export function conditionState(condition: Condition, steps: TaskStep[], gates: UserGate[], waits: TaskWait[]): ConditionState {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const leaf = (id: string, on: "completed" | "verified" | "passed"): ConditionState => {
    const step = byId.get(id);
    if (!step || step.status === "failed" || step.status === "skipped") return "unsatisfiable";
    const gate = gates.find((item) => item.step_id === id);
    if (gate) {
      if (gate.state !== "passed" && gate.state !== "cancelled") return "pending";
      return on === "passed" && gate.state !== "passed" ? "unsatisfiable" : "satisfied";
    }
    const wait = waits.find((item) => item.step_id === id);
    if (wait) {
      if (wait.state !== "done") return "pending";
      return on === "passed" && wait.outcome !== "ready" && wait.outcome !== "resumed" ? "unsatisfiable" : "satisfied";
    }
    if (step.kind === "review" && step.status === "verified") return on === "passed"
      && !step.result?.passed && step.result?.status !== "passed_by_adjudication" ? "unsatisfiable" : "satisfied";
    if (step.status === "verified") return "satisfied";
    return on === "completed" && (step.status === "completed" || step.status === "deciding" && step.result?.status === "completed")
      ? "satisfied" : "pending";
  };
  const evaluate = (cond: Condition): ConditionState => {
    if (typeof cond === "string") return leaf(cond, "verified");
    if ("step" in cond) return leaf(cond.step, cond.on);
    const children = "all_of" in cond ? cond.all_of : "any_of" in cond ? cond.any_of : cond.of;
    const k = "all_of" in cond ? children.length : "any_of" in cond ? 1 : cond.k_of;
    const states = children.map(evaluate);
    const satisfied = states.filter((state) => state === "satisfied").length;
    if (satisfied >= k) return "satisfied";
    return satisfied + states.filter((state) => state === "pending").length < k ? "unsatisfiable" : "pending";
  };
  return evaluate(condition);
}

/** Each step's depth in the plan's dependency graph: 0 without dependencies, else one more than its deepest. A
 * dependency outside `steps` does not count. */
export function stepLevels(steps: TaskStep[]): Map<string, number> {
  const items = layoutItems(steps);
  const byId = new Map(items.map((s) => [s.id, s]));
  const levels = new Map<string, number>();
  const visiting = new Set<string>();
  const level = (id: string): number => {
    const known = levels.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 0; // a cycle the plan's checks never let through
    visiting.add(id);
    const deps = (byId.get(id)?.depends_on ?? []).filter((d) => byId.has(d));
    const value = deps.length ? 1 + Math.max(...deps.map(level)) : 0;
    visiting.delete(id);
    levels.set(id, value);
    return value;
  };
  items.forEach((s) => level(s.id));
  return levels;
}

/** Each step's centre across its row, `stepX` apart: under the mean place of the steps it depends on, then in plan
 * order. The live steps are centred and the dropped ones follow on their right, so no live edge runs behind a
 * dropped step. */
export function stepColumns(steps: TaskStep[], levels: Map<string, number>, stepX: number): Map<string, number> {
  const items = layoutItems(steps);
  const x = new Map<string, number>();
  const depth = Math.max(...levels.values());
  for (let row = 0; row <= depth; row++) {
    const placed = items
      .filter((s) => levels.get(s.id) === row)
      .map((s) => {
        const parents = (s.depends_on ?? []).map((d) => x.get(d)).filter((v): v is number => v !== undefined);
        return { s, at: parents.length ? parents.reduce((a, b) => a + b, 0) / parents.length : 0 };
      })
      .sort((a, b) => a.at - b.at || a.s.step_index - b.s.step_index);
    const live = placed.filter(({ s }) => s.status !== "skipped");
    const dropped = placed.filter(({ s }) => s.status === "skipped");
    const left = -((live.length - 1) * stepX) / 2;
    live.forEach(({ s }, i) => x.set(s.id, left + i * stepX));
    const after = live.length ? left + live.length * stepX : stepX;
    dropped.forEach(({ s }, i) => x.set(s.id, after + i * stepX));
  }
  return x;
}

const HIDE_SKIPPED_KEY = "looper.pipeline.hide-skipped";

/** Whether this viewer hides the dropped steps: what they chose last, hidden when they never chose. */
export function storedHideSkipped(): boolean {
  try {
    return window.localStorage.getItem(HIDE_SKIPPED_KEY) !== "false";
  } catch {
    return true;
  }
}

export function storeHideSkipped(hide: boolean) {
  try {
    window.localStorage.setItem(HIDE_SKIPPED_KEY, String(hide));
  } catch {
    // A per-viewer convenience: without storage the next visit hides them again.
  }
}
