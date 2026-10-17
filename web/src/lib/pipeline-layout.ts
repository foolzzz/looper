/** Where the Pipeline tab draws a plan's steps: a row per depth in the dependency graph, and a place in it. */
import type { TaskStep } from "@/lib/api";

/** The steps the Pipeline draws: every one, or all but the ones a replan dropped (skipped); `hidden` counts those
 * left out. */
export function shownSteps(steps: TaskStep[], hideSkipped: boolean): { steps: TaskStep[]; hidden: number } {
  if (!hideSkipped) return { steps, hidden: 0 };
  const shown = steps.filter((s) => s.status !== "skipped");
  return { steps: shown, hidden: steps.length - shown.length };
}

/** Each step's depth in the plan's dependency graph: 0 without dependencies, else one more than its deepest. A
 * dependency outside `steps` does not count. */
export function stepLevels(steps: TaskStep[]): Map<string, number> {
  const byId = new Map(steps.map((s) => [s.id, s]));
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
  steps.forEach((s) => level(s.id));
  return levels;
}

/** Each step's centre across its row, `stepX` apart: under the mean place of the steps it depends on, then in plan
 * order. The live steps are centred and the dropped ones follow on their right, so no live edge runs behind a
 * dropped step. */
export function stepColumns(steps: TaskStep[], levels: Map<string, number>, stepX: number): Map<string, number> {
  const x = new Map<string, number>();
  const depth = Math.max(...levels.values());
  for (let row = 0; row <= depth; row++) {
    const placed = steps
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
