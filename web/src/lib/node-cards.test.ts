import { describe, expect, it } from "vitest";
import { type CardLook, type CardSection, clipText, formatWhen, gateCard, plannerCard, resultCard, stepCard } from "@/lib/node-cards";
import type { TaskDetail, TaskStep, UserGate } from "@/lib/api";

const look = (label: string): CardLook => ({ label, border: "b", borderStyle: "solid", background: "bg", color: "c" });
const LOOKS: Record<string, CardLook> = { verified: look("Done"), failed: look("Failed"), pending: look("Not started") };

function step(id: string, extra: Partial<TaskStep> = {}): TaskStep {
  return {
    id, task_id: "t1", step_index: 0, step_key: id, kind: "work", instruction: "", acceptance_criteria: [], target_files: [],
    depends_on: [], status: "pending", review: false, role_key: null, review_role_key: null, review_role_required: true,
    role_version: null, review_role_version: null, review_stage: null, retries: 0, worker_id: null, reviewer_id: null,
    result: null, created_at: "2026-10-17T08:00:00Z", updated_at: "2026-10-17T08:00:00Z", completed_at: null,
    ...extra,
  };
}

const LONG = Array.from({ length: 12 }, (_, i) => `line ${i + 1} of what the Worker did`).join("\n");

function cardOf(s: TaskStep, steps: TaskStep[] = [s], skippedBecause?: string) {
  return stepCard(s, { taskId: "t1", look: LOOKS[s.status] ?? look(s.status), lookOf: (d) => LOOKS[d.status],
                       stepsById: new Map(steps.map((d) => [d.id, d])), skippedBecause });
}
const titles = (sections: CardSection[]) => sections.map((section) => section.title);
const section = (sections: CardSection[], title: string) => sections.find((s) => s.title === title);

describe("stepCard", () => {
  it("shows every part of a done step in order", () => {
    const s1 = step("s1", { status: "verified" });
    const s2 = step("s2", {
      status: "verified", instruction: "Write test_greet.py.\nRun it.", acceptance_criteria: ["the test passes"],
      target_files: ["test_greet.py"], depends_on: ["s1"], role_key: "tester", role_version: 2,
      review_role_key: "security", review_role_version: 1, review_role_required: false, review: true, retries: 1,
      worker_id: "worker-a", reviewer_id: "reviewer-b", completed_at: "2026-10-17T08:05:00Z",
      result: { status: "completed", summary: "Wrote the test; it passes." },
    });
    const card = cardOf(s2, [s1, s2]);
    expect(card).toMatchObject({ name: "s2", look: LOOKS.verified,
                                 roles: ["tester · work v2", "security · review v1 (advisory)"] });
    expect(titles(card.sections)).toEqual(["Instruction", "Acceptance criteria", "Files", "Depends on", "Runs", "Result", "Timing"]);
    expect(section(card.sections, "Instruction")).toMatchObject({ text: "Write test_greet.py.\nRun it." });
    expect(section(card.sections, "Acceptance criteria")).toMatchObject({ items: ["the test passes"] });
    expect(section(card.sections, "Files")).toMatchObject({ items: ["test_greet.py"], mono: true });
    expect(section(card.sections, "Depends on")).toEqual({ kind: "steps", title: "Depends on", steps: [{ name: "s1", look: LOOKS.verified }] });
    expect(section(card.sections, "Runs")).toEqual({ kind: "facts", title: "Runs", facts: [
      { label: "Worker", value: "worker-a", href: "#history/worker-a?task=t1" },
      { label: "Reviewer", value: "reviewer-b", href: "#history/reviewer-b?task=t1" },
      { label: "Retries", value: "1" },
      { label: "Review", value: "Needs a passing review" },
    ] });
    expect(section(card.sections, "Result")).toMatchObject({ text: "Wrote the test; it passes.", preview: undefined });
    expect(section(card.sections, "Timing")).toMatchObject({ facts: [
      { label: "Planned", value: formatWhen("2026-10-17T08:00:00Z") },
      { label: "First finished", value: formatWhen("2026-10-17T08:05:00Z") },
    ] });
  });

  it("leaves out the empty parts", () => {
    const card = cardOf(step("s1", { instruction: "Do it." }));
    expect(card.roles).toEqual([]);
    expect(titles(card.sections)).toEqual(["Instruction", "Timing"]);
  });

  it("clips a long result to a preview and keeps the whole text", () => {
    const card = cardOf(step("s1", { status: "verified", result: { status: "completed", summary: LONG } }));
    const result = section(card.sections, "Result");
    expect(result).toMatchObject({ text: LONG });
    const preview = (result as { preview?: string }).preview!;
    expect(preview.split("\n")).toHaveLength(4);
    expect(preview.endsWith("…")).toBe(true);
    expect(LONG.startsWith(preview.slice(0, -1))).toBe(true);
  });

  it("says why a failed attempt failed, and which review did not pass a step", () => {
    const failed = cardOf(step("s3", { status: "failed", result: { status: "failed", error: "the build broke", summary: "tried" } }));
    expect(section(failed.sections, "Result: failed")).toMatchObject({ text: "the build broke", tone: "destructive" });
    expect(section(failed.sections, "Result: failed")).not.toHaveProperty("markdown");

    const reviewed = cardOf(step("s4", { status: "failed", result: {
      status: "completed", summary: "Done.", verify_passed: false, verify_findings: ["no tests", "no docs"] } }));
    expect(titles(reviewed.sections)).toContain("Result");
    expect(section(reviewed.sections, "Review")).toMatchObject({ text: "The review did not pass it:\nno tests\nno docs" });

    const byRole = cardOf(step("s5", { status: "verified", result: {
      status: "completed", summary: "Done.", verify_passed: true,
      role_review: { role: "security", passed: false, findings: [], summary: "shell injection" } } }));
    expect(section(byRole.sections, "Review")).toMatchObject({ text: "Review by role security did not pass it:\nshell injection" });
  });

  it("shows no result for a running step, whose result is its previous attempt's", () => {
    const card = cardOf(step("s1", { status: "dispatched", result: { status: "failed", error: "old" } }));
    expect(titles(card.sections)).not.toContain("Result: failed");
  });

  it("says what a step in review waits for", () => {
    const card = cardOf(step("s1", { status: "completed", review_stage: "role", review_role_key: "security" }));
    expect(section(card.sections, "Runs")).toMatchObject({ facts: [{ label: "Now", value: "Waiting for review: security" }] });
  });

  it("gives a dropped step's reason, or the default one", () => {
    const why = "replan at 10:00: the user asked for it. Plan changed; dropped s4";
    expect(section(cardOf(step("s4", { status: "skipped" }), undefined, why).sections, "Skipped")).toMatchObject({ text: why });
    const dropped = cardOf(step("s4", { status: "skipped", completed_at: "2026-10-17T08:05:00Z" }));
    expect(section(dropped.sections, "Skipped")).toMatchObject({ text: "Dropped from the plan by a replan" });
    expect(section(dropped.sections, "Timing")).toMatchObject({ facts: [{ label: "Planned" }, { label: "Dropped" }] });
  });
});

describe("clipText", () => {
  it("leaves short text alone and cuts long lines", () => {
    expect(clipText("one\ntwo")).toBeUndefined();
    expect(clipText("x".repeat(400))).toBe(`${"x".repeat(320)}…`);
  });
});

const task = (extra: Partial<TaskDetail> = {}) =>
  ({ task_id: "t1", status: "running", planner_id: "planner-t1", steps: [step("s1"), step("s2"), step("s3", { status: "skipped" })],
     result_summary: null,
     error: null, created_at: "2026-10-17T08:00:00Z", completed_at: null, ...extra }) as TaskDetail;

describe("plannerCard and resultCard", () => {
  it("names the Planner with its history, the plan's size (dropped steps apart) and its latest note", () => {
    const card = plannerCard(task({ progress: [{ done: "", now: "s2 runs", next: "", risks: "", estimate: "", at: "2026-10-17T08:01:00Z" }] }), look("Planning"));
    expect(card.sections).toEqual([
      { kind: "facts", title: "Runs", facts: [
        { label: "Planner", value: "planner-t1", href: "#history/planner-t1?task=t1" },
        { label: "Plan", value: "2 steps, 1 dropped" },
      ] },
      { kind: "text", title: `Latest note · ${formatWhen("2026-10-17T08:01:00Z")}`, text: "Now: s2 runs", preview: undefined },
    ]);
  });

  it("gives the task's error, and how long it ran, once it ended", () => {
    const card = resultCard(task({ status: "failed", error: "s3 failed", completed_at: "2026-10-17T08:02:05Z" }), look("Failed"));
    expect(titles(card.sections)).toEqual(["Error", "Timing"]);
    expect(section(card.sections, "Error")).toMatchObject({ text: "s3 failed", tone: "destructive", markdown: true });
    expect(section(card.sections, "Timing")).toMatchObject({ facts: [
      { label: "Created", value: formatWhen("2026-10-17T08:00:00Z") },
      { label: "Ended", value: formatWhen("2026-10-17T08:02:05Z") },
      { label: "Took", value: "2m 5s" },
    ] });
  });

  it("clips a long summary", () => {
    const card = resultCard(task({ status: "completed", result_summary: LONG }), look("Completed"));
    expect(section(card.sections, "Summary")).toMatchObject({ text: LONG, preview: clipText(LONG), markdown: true });
  });
});

function gate(extra: Partial<UserGate> = {}): UserGate {
  return {
    step: "g1", step_id: "g1", state: "discussing", topic: "Pick the database", agenda: [], findings: "", summary: null,
    resolution: "", ended_by_user: false, plan_change: null, depends_on: ["s1"], waiting_on: null, not_delivered: 0,
    retry_at: null, turns: 0, reminders: 0, opened_at: null, first_reply_at: null, closed_at: null, ...extra,
  };
}

describe("gateCard", () => {
  const s1 = step("s1", { status: "verified" });
  const g1 = step("g1", { kind: "gate", status: "waiting_user", instruction: "Pick the database", depends_on: ["s1"] });
  const cardOfGate = (g?: UserGate) =>
    gateCard(g1, g, { look: look("Your turn"), lookOf: (d) => LOOKS[d.status], stepsById: new Map([[s1.id, s1], [g1.id, g1]]) });

  it("shows the topic, the agenda with each question's state, whom it waits on and when it opened", () => {
    const card = cardOfGate(gate({
      agenda: [
        { id: "q1", question: "Postgres or SQLite?", options: ["Postgres", "SQLite"], status: "resolved", answer: "Postgres" },
        { id: "q2", question: "Keep the old table?", options: [], status: "open", answer: "" },
        { id: "q3", question: "Shard it?", options: [], status: "dropped", answer: "" },
      ],
      waiting_on: "user", not_delivered: 2, turns: 3, findings: "The old table has 2M rows.", opened_at: "2026-10-17T08:00:00Z",
    }));
    expect(card).toMatchObject({ name: "g1", look: { label: "Your turn" }, roles: [] });
    expect(titles(card.sections)).toEqual(["Topic", "Agenda", "Discussion", "Findings", "Depends on", "Timing"]);
    expect(section(card.sections, "Agenda")).toMatchObject({ items: [
      "Resolved: Postgres or SQLite? → Postgres", "Open: Keep the old table?", "Dropped: Shard it?"] });
    expect(section(card.sections, "Discussion")).toMatchObject({ facts: [
      { label: "Waiting on", value: "You" }, { label: "Not delivered", value: "2 messages" }, { label: "Turns", value: "3" }] });
    expect(section(card.sections, "Depends on")).toMatchObject({ steps: [{ name: "s1", look: LOOKS.verified }] });
  });

  it("shows what a passed gate settled, and why a cancelled one was cancelled", () => {
    const passed = cardOfGate(gate({ state: "passed", closed_at: "2026-10-17T09:00:00Z",
                                     summary: { decisions: ["Use Postgres"], assumptions: [], open: ["Backups"] } }));
    expect(titles(passed.sections)).toEqual(["Topic", "Decisions", "Still open", "Depends on", "Timing"]);
    expect(section(passed.sections, "Timing")).toMatchObject({ facts: [{ label: "Closed" }] });
    const cancelled = cardOfGate(gate({ state: "cancelled", resolution: "The user asked to stop." }));
    expect(section(cancelled.sections, "Cancelled")).toMatchObject({ text: "The user asked to stop." });
    expect(section(cardOfGate(gate({ waiting_on: "planner" })).sections, "Discussion"))
      .toMatchObject({ facts: [{ label: "Waiting on", value: "The Planner" }] });
  });

  it("shows the step's topic before the gate has a row", () => {
    expect(titles(cardOfGate(undefined).sections)).toEqual(["Topic", "Depends on"]);
  });
});
