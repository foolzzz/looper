import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StepConsults } from "@/components/task-advice";
import type { AdvisorConsult } from "@/lib/api";

const ANSWERED: AdvisorConsult = {
  id: 1, step_id: "step-a", source: "checkpoint", kind: "plan_review", attempt: 1, state: "answered", failure: null,
  rounds: 2, returns: 1, spent_usd: 0.2104, status: "answered",
  answers: [{ question_id: "q1", conclusion: "Keep the old column until the switch is verified.", confidence: "high" }],
  adoption: "partial", unlocked: true, created_at: "2026-11-19T10:00:00+00:00", finished_at: "2026-11-19T10:01:00+00:00",
};

const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/&#x27;/g, "'").replace(/\s+/g, " ");

describe("StepConsults", () => {
  it("shows where a consult came from, its conclusions, what was taken and whether the attempt passed", () => {
    const shown = text(renderToStaticMarkup(<StepConsults consults={[ANSWERED]} />));
    expect(shown).toContain("Plan review before the step started · for attempt 1");
    expect(shown).toContain("Answered");
    expect(shown).toContain("q1 Keep the old column until the switch is verified. · confidence high");
    expect(shown).toContain("Planner adopted part of it");
    expect(shown).toContain("Attempt 1 passed after it");
    expect(shown).toContain("2 rounds · request returned 1×");
  });

  it("says why a consult gave no answer, in words, and nothing before the attempt is judged", () => {
    const blocked: AdvisorConsult = { ...ANSWERED, id: 2, source: "optional", attempt: 3, state: "blocked",
      failure: "blocked", answers: [], adoption: null, unlocked: null, status: null, rounds: 0, returns: 2 };
    const failed: AdvisorConsult = { ...blocked, id: 3, state: "failed", failure: "refused" };
    const shown = text(renderToStaticMarkup(<StepConsults consults={[blocked, failed]} />));
    expect(shown).toContain("Asked by the Planner after a failed attempt · for attempt 3");
    expect(shown).toContain("Blocked: the request failed its checks");
    expect(shown).toContain("Failed (refused)");
    expect(shown).not.toContain("passed");
    expect(shown).not.toContain("adopted");
    expect(renderToStaticMarkup(<StepConsults consults={[]} />)).toBe("");
  });
});
