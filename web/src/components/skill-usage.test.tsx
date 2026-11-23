// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { SkillUsage, type SkillUsageState } from "@/components/skill-usage";
import type { SkillRate, SkillUsagePeriod, SkillUsageRow } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const rate = (over: Partial<SkillRate>): SkillRate => ({
  state: "ok", value: 0.5, low: 0.3, high: 0.7, n: 40, successes: 20, ...over,
});

const period = (over: Partial<SkillUsagePeriod> = {}): SkillUsagePeriod => ({
  loaded_runs: 30,
  loaded_attempts: 30,
  invoked: { count: 12, n: 25, unknown: 5, rate: 0.48 },
  effect: {
    first_pass: rate({ n: 30, successes: 21, value: 0.7, low: 0.52, high: 0.83 }),
    control: rate({ n: 50, successes: 20, value: 0.4, low: 0.28, high: 0.54 }),
    cost: { loaded: { avg: 0.42, n: 21 }, control: { avg: 0.55, n: 20 } },
  },
  ...over,
});

const row = (over: Partial<SkillUsageRow> = {}): SkillUsageRow => ({
  ...period(),
  skill_id: "skill-1", name: "deploy-check", sha256: "abc", lifecycle: "active", versions: [1, 3],
  comparison: period({ loaded_attempts: 4, loaded_runs: 4 }),
  review: [],
  window: { start: "2026-11-13T00:00:00+00:00", end: "2026-11-20T00:00:00+00:00" },
  comparison_window: { start: "2026-10-16T00:00:00+00:00", end: "2026-11-13T00:00:00+00:00" },
  coverage_since: "2026-11-01T00:00:00+00:00",
  note: "Observational data, not a controlled experiment: skills are often added to harder tasks.",
  ...over,
});

const state = (r: SkillUsageRow | null, error = ""): SkillUsageState => ({
  rows: r ? new Map([[r.skill_id, r]]) : error ? null : new Map(), error,
});

let host: HTMLElement;
let root: Root;
const show = (usage: SkillUsageState) => act(() => root.render(<SkillUsage skillId="skill-1" usage={usage} />));
const text = () => host.textContent ?? "";

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

describe("SkillUsage", () => {
  it("shows loads, calls and the first-pass rate with its interval", () => {
    show(state(row()));
    expect(text()).toContain("30 attempts loaded it");
    expect(text()).toContain("called in 12 of 25 (48%), 5 unknown");
    expect(text()).toContain("first pass 70% (21 of 30, 95% 52%–83%)");
    expect(text()).not.toContain("Suggested review");
  });

  it("says unknown, never 0, where the runtime cannot tell", () => {
    show(state(row({ invoked: { count: 0, n: 0, unknown: 8, rate: null } })));
    expect(text()).toContain("calls unknown for this runtime");
    expect(text()).not.toContain("called in 0");
  });

  it("shows counts and no rate under 20 attempts on a side", () => {
    show(state(row({
      effect: {
        first_pass: rate({ state: "insufficient", value: null, low: null, high: null, n: 19, successes: 12 }),
        control: rate({ state: "insufficient", value: null, low: null, high: null, n: 60, successes: 30 }),
        cost: { loaded: { avg: null, n: 12 }, control: { avg: null, n: 30 } },
      },
    })));
    expect(text()).toContain("first pass: not enough data (19 with it, 60 without; 20 needed on each side)");
    expect(text()).not.toContain("%, 95%");
  });

  it("says attempts are not recorded where there is no step_attempts table", () => {
    show(state(row({ loaded_attempts: null, effect: null, loaded_runs: 6 })));
    expect(text()).toContain("6 runs loaded it");
    expect(text()).toContain("first pass: attempts are not recorded here");
  });

  it("says so when no run loaded it", () => {
    show(state(row({ loaded_runs: 0, loaded_attempts: 0 })));
    expect(text()).toContain("not loaded by any run");
  });

  it("flags a suggested review and lists the reason, the control and the note behind the toggle", () => {
    show(state(row({ review: ["never_invoked", "divergent"] })));
    expect(text()).toContain("Suggested review");
    expect(text()).not.toContain("Loaded by more than 30");
    const toggle = host.querySelector("button") as HTMLButtonElement;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    act(() => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(text()).toContain("Suggested review: Loaded by more than 30 attempts and never called.");
    expect(text()).toContain("Suggested review: First-pass interval differs from attempts without it.");
    expect(text()).toContain("Without it: 40% (20 of 50, 95% 28%–54%)");
    expect(text()).toContain("with it $0.42, without it $0.55");
    expect(text()).toContain("Versions with this content: 1, 3.");
    expect(text()).toContain("not a controlled experiment");
  });

  it("shows an error as text and a missing row as unavailable", () => {
    show(state(null, "boom"));
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("Usage unavailable: boom");
    show(state(null));
    expect(text()).toContain("Usage: not available for this skill.");
    show({ rows: null, error: "" });
    expect(text()).toContain("Loading usage");
  });

  it("wraps long text instead of overflowing a narrow column", () => {
    show(state(row()));
    const button = host.querySelector("button") as HTMLButtonElement;
    expect(button.className).toContain("min-w-0");
    expect(button.querySelector("span")?.className).toContain("break-words");
  });
});
