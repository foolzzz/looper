import { describe, expect, it } from "vitest";
import type { AdvisorConsult, LlmRecord } from "@/lib/api";
import {
  CHECKPOINT_UNFINISHED,
  consultCard,
  consultLabel,
  consultPlaces,
  consultRunTree,
  consultTone,
  unfinishedCheckpoints,
} from "@/lib/consults";
import type { CardLook, CardSection } from "@/lib/node-cards";

const LOOK: CardLook = { label: "", border: "b", borderStyle: "dashed", background: "bg", color: "c" };

const ANSWERED: AdvisorConsult = {
  id: 1, step_id: "step-a", source: "optional", kind: "diagnose", attempt: 2, state: "answered", failure: null,
  rounds: 2, returns: 1, spent_usd: 0.42, status: "answered", checks: [{ stage: "code", category: "url", count: 1 }],
  answers: [{ question_id: "q1", conclusion: "Pin the client version.", confidence: "high" }],
  adoption: "adopted", unlocked: true,
  question: {
    kind: "diagnose", problem: "A test fails only in CI.", goal: "Make the suite pass.",
    acceptance: ["The suite passes twice in a row"], constraints: ["No new dependency"],
    attempts: [{ approach: "Re-ran it", signal: "same failure", learned: "not flaky" }],
    artifacts: [{ kind: "log", lang: "text", content: "AssertionError: expected 2, got 3" }],
    questions: [{ id: "q1", text: "What makes CI differ?" }],
    prior_rounds: [{ needs_info_id: "n1", answer: "CI runs Python 3.12." }],
  },
  needs_info: [{ id: "n1", question: "Which Python does CI run?", why: "the behaviour changed in 3.12" }],
  advice: {
    status: "answered",
    answers: [{ question_id: "q1", conclusion: "Pin the client version.", basis: ["3.12 changed the default"], confidence: "high",
                assumptions: ["the lock file is used"] }],
    options: [], next_steps: [{ id: "s1", action: "Pin it", verify: "the suite passes" }], risks: ["an older client may lack a fix"],
  },
  adoption_items: [{ ref: "q1", use: "adopted", why: "it explains the failure" }],
  created_at: "2026-11-19T10:05:00+00:00", finished_at: "2026-11-19T10:06:30+00:00", duration_s: 90,
};

const section = (sections: CardSection[], title: string) => sections.find((s) => s.title === title);
const shown = (sections: CardSection[]) => JSON.stringify(sections);

describe("consult labels", () => {
  it("names a consult by its kind and how it ended, and colours it by state", () => {
    expect(consultLabel(ANSWERED)).toBe("诊断 · 已采纳");
    expect(consultTone(ANSWERED)).toBe("answered");
    const asking = { ...ANSWERED, state: "asking" as const, adoption: null, status: null };
    expect([consultLabel(asking), consultTone(asking)]).toEqual(["诊断 · 请教中", "live"]);
    const drafting = { ...asking, state: "drafting" as const, kind: null };
    expect(consultLabel(drafting)).toBe("请教 · 请教中");
    expect(consultLabel({ ...asking, state: "needs_info" })).toBe("诊断 · 回问中");
    const blocked = { ...asking, state: "blocked" as const };
    expect([consultLabel(blocked), consultTone(blocked)]).toEqual(["诊断 · 被拦", "failed"]);
    const refused = { ...ANSWERED, status: "out_of_scope" as const, adoption: null };
    expect([consultLabel(refused), consultTone(refused)]).toEqual(["诊断 · 拒答", "failed"]);
    expect(consultLabel({ ...ANSWERED, kind: "plan_review", adoption: null })).toBe("计划评审 · 已回答");
  });
});

describe("consultPlaces", () => {
  it("puts the plan's consults by the plan, a checkpoint before its step and the others after it, in time order", () => {
    const plan = { ...ANSWERED, id: 5, step_id: null, created_at: "2026-11-19T09:00:00+00:00" };
    const checkpoint = { ...ANSWERED, id: 2, source: "checkpoint" as const, created_at: "2026-11-19T09:30:00+00:00" };
    const later = { ...ANSWERED, id: 3, source: "ladder" as const, created_at: "2026-11-19T11:00:00+00:00" };
    const places = consultPlaces([later, ANSWERED, checkpoint, plan]);
    expect(places.plan.map((c) => c.id)).toEqual([5]);
    expect(places.before.get("step-a")?.map((c) => c.id)).toEqual([2]);
    expect(places.after.get("step-a")?.map((c) => c.id)).toEqual([1, 3]);
  });

  it("marks a step whose checkpoint consult ended without an answer, not one still going or answered", () => {
    const failed = { ...ANSWERED, id: 2, step_id: "step-b", source: "checkpoint" as const, state: "failed" as const };
    const going = { ...ANSWERED, id: 3, step_id: "step-c", source: "checkpoint" as const, state: "asking" as const };
    const answered = { ...ANSWERED, id: 4, step_id: "step-d", source: "checkpoint" as const };
    const optional = { ...failed, id: 6, step_id: "step-e", source: "optional" as const };
    expect([...unfinishedCheckpoints([failed, going, answered, optional])]).toEqual(["step-b"]);
    expect(CHECKPOINT_UNFINISHED).toBe("检查点请教未完成");
  });
});

describe("consultCard", () => {
  it("shows the request, the questions back and their answers, the advice, the stance, the cost and the time", () => {
    const card = consultCard(ANSWERED, LOOK);
    expect(card.name).toBe("诊断 · 已采纳");
    const titles = card.sections.map((s) => s.title);
    expect(titles).toEqual(["Consult", "Checks the request failed", "Problem", "Goal", "Questions", "Acceptance",
      "Constraints", "Tried", "Material: log (text)", "The advisor asked back", "The Planner answered",
      "Answer q1 · confidence high", "Next steps", "Risks", "The Planner's stance", "Why", "Cost and time"]);
    expect(section(card.sections, "The advisor asked back")).toMatchObject(
      { items: ["n1: Which Python does CI run? (the behaviour changed in 3.12)"] });
    expect(section(card.sections, "The Planner answered")).toMatchObject({ items: ["n1: CI runs Python 3.12."] });
    expect(section(card.sections, "Answer q1 · confidence high")).toMatchObject({
      text: "Pin the client version.\nBasis: 3.12 changed the default\nAssumes: the lock file is used" });
    expect(section(card.sections, "Why")).toMatchObject({ items: ["q1: adopted: it explains the failure"] });
    const cost = shown([section(card.sections, "Cost and time")!]);
    expect(cost).toContain("$0.42");
    expect(cost).toContain("1m 30s");
    expect(shown([section(card.sections, "The Planner's stance")!])).toContain("2 passed after it");
  });

  it("shows a blocked consult's checks by category and count only", () => {
    const blocked: AdvisorConsult = { ...ANSWERED, state: "blocked", failure: "blocked", status: null, adoption: null,
      unlocked: null, question: null, advice: null, needs_info: [], adoption_items: [], answers: [], rounds: 0, returns: 3,
      checks: [{ stage: "code", category: "local_path", count: 3 }, { stage: "judge", category: "internal_name", count: 1 }] };
    const card = consultCard(blocked, LOOK);
    expect(card.sections.map((s) => s.title)).toEqual(["Consult", "Checks the request failed", "Cost and time"]);
    expect(section(card.sections, "Checks the request failed")).toMatchObject({ facts: [
      { label: "a local path", value: "code check · 3×" }, { label: "an internal name", value: "judge check · 1×" }] });
    expect(shown(card.sections)).toContain("Blocked: the request failed its checks");
  });
});

describe("consultRunTree", () => {
  const run = (id: number, agent: string) => ({ id, agent_id: agent, created_at: `2026-11-19T10:0${id}:00Z` }) as LlmRecord;
  it("puts each consult run under the Planner run that started it, and one whose Planner run is unlisted last", () => {
    const planner = [run(1, "planner-t"), run(2, "planner-t")];
    const tree = consultRunTree(planner, [
      { record: run(5, "advisor-x"), kind: "advisor", parentId: 2 },
      { record: run(4, "advice_check-x"), kind: "advice_check", parentId: 2 },
      { record: run(6, "advisor-y"), kind: "advisor", parentId: 9 },
    ]);
    expect(tree.map((b) => [b.parent?.id ?? null, b.runs.map((r) => r.record.id)])).toEqual([[2, [4, 5]], [null, [6]]]);
  });
});
