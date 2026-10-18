import { describe, expect, it } from "vitest";
import {
  type CardLook,
  type CardSection,
  clipText,
  formatSeconds,
  formatWhen,
  gateCard,
  plannerCard,
  resultCard,
  stepCard,
  stepSpan,
} from "@/lib/node-cards";
import type { StepWorkspace, TaskDetail, TaskStep, UserGate } from "@/lib/api";

const look = (label: string): CardLook => ({ label, border: "b", borderStyle: "solid", background: "bg", color: "c" });
const LOOKS: Record<string, CardLook> = { verified: look("Done"), failed: look("Failed"), pending: look("Not started") };

function step(id: string, extra: Partial<TaskStep> = {}): TaskStep {
  return {
    id, task_id: "t1", step_index: 0, step_key: id, kind: "work", instruction: "", acceptance_criteria: [], target_files: [],
    depends_on: [], status: "pending", review: false, role_key: null, role_version: null, review_roles: [],
    review_stage: null, review_position: null, retries: 0, worker_id: null, reviewer_id: null,
    result: null, created_at: "2026-10-17T08:00:00Z", updated_at: "2026-10-17T08:00:00Z", completed_at: null, started_at: null,
    ...extra,
  };
}

const LONG = Array.from({ length: 12 }, (_, i) => `line ${i + 1} of what the Worker did`).join("\n");

function cardOf(
  s: TaskStep, steps: TaskStep[] = [s], skippedBecause?: string, workspaces?: StepWorkspace[], active = true,
) {
  return stepCard(s, { taskId: "t1", active, look: LOOKS[s.status] ?? look(s.status), lookOf: (d) => LOOKS[d.status],
                       stepsById: new Map(steps.map((d) => [d.id, d])), skippedBecause, workspaces });
}
const titles = (sections: CardSection[]) => sections.map((section) => section.title);
const section = (sections: CardSection[], title: string) => sections.find((s) => s.title === title);

function worktree(id: number, extra: Partial<StepWorkspace> = {}): StepWorkspace {
  return {
    id, step_id: "s1", step_key: "s1", branch: `looper/t1/s1-${id}`, target_branch: "main", base_commit: "b".repeat(40),
    checkpoint: false, result_commit: null, merge_commit: null, state: "active", merge_error: null, merge_files: [],
    detail: "", links: [], path: "/w", removed_at: null, discardable: true, ...extra,
  };
}

describe("stepCard", () => {
  it("shows the step's latest worktree after its runs", () => {
    const earlier = worktree(2, { state: "discarded" });
    const latest = worktree(3, { state: "unmerged", merge_error: "local_changes", merge_files: ["base.txt"],
                                 checkpoint: true, links: [".venv", ".env"] });
    const card = cardOf(step("s1", { status: "failed", worker_id: "worker-a" }), undefined, undefined, [earlier, latest]);
    expect(titles(card.sections)).toEqual(["Runs", "Worktree", "Files in the way of the merge", "Timing"]);
    expect(section(card.sections, "Worktree")).toEqual({ kind: "facts", title: "Worktree", facts: [
      { label: "Branch", value: "looper/t1/s1-3" },
      { label: "State", value: "Not merged: local_changes" },
      { label: "From", value: "main at bbbbbbbbbbbb (checkpoint)" },
      { label: "Why", value: "Uncommitted changes in the task directory were in the way" },
      { label: "Linked", value: ".venv, .env" },
      { label: "Earlier", value: "1 worktree" },
    ] });
    expect(section(card.sections, "Files in the way of the merge")).toMatchObject({ items: ["base.txt"], mono: true });
    const merged = cardOf(step("s1", { status: "verified" }), undefined, undefined,
                          [worktree(1, { state: "merged", merge_commit: "c".repeat(40) })]);
    expect(section(merged.sections, "Worktree")).toMatchObject({ facts: [
      { label: "Branch", value: "looper/t1/s1-1" }, { label: "State", value: "Merged as cccccccccccc" },
      { label: "From", value: "main at bbbbbbbbbbbb" }] });
    expect(titles(cardOf(step("s1")).sections)).not.toContain("Worktree");
  });

  it("shows every part of a done step in order", () => {
    const s1 = step("s1", { status: "verified" });
    const s2 = step("s2", {
      status: "verified", instruction: "Write test_greet.py.\nRun it.", acceptance_criteria: ["the test passes"],
      target_files: ["test_greet.py"], depends_on: ["s1"], role_key: "tester", role_version: 2,
      review_roles: [{ role: "security", required: false, version: 1 }, { role: "perf", required: true, version: null }],
      review: true, retries: 1,
      worker_id: "worker-a", reviewer_id: "reviewer-b", started_at: "2026-10-17T08:01:00Z", completed_at: "2026-10-17T08:05:00Z",
      result: { status: "completed", summary: "Wrote the test; it passes." },
    });
    const card = cardOf(s2, [s1, s2]);
    expect(card).toMatchObject({ name: "s2", look: LOOKS.verified,
                                 roles: ["tester · work v2", "security · review v1 (advisory)", "perf · review"] });
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
      { label: "Started", value: formatWhen("2026-10-17T08:01:00Z") },
      { label: "Finished", value: formatWhen("2026-10-17T08:05:00Z") },
      { label: "Took", value: "4m 0s" },
    ] });
  });

  it("shows a step running again as running, without the finish of its earlier attempt", () => {
    const retried = cardOf(step("s1", { status: "dispatched", retries: 1, started_at: "2026-10-17T08:00:00Z",
                                        completed_at: "2026-10-17T08:05:00Z" }));
    expect(section(retried.sections, "Timing")).toMatchObject({ facts: [
      { label: "Planned" }, { label: "Started" }, { label: "Running for" }] });
    const stopped = cardOf(step("s1", { status: "dispatched", started_at: "2026-10-17T08:00:00Z" }),
                           undefined, undefined, undefined, false);
    expect(section(stopped.sections, "Timing")).toMatchObject({ facts: [{ label: "Planned" }, { label: "Started" }] });
  });

  it("leaves out the empty parts", () => {
    const card = cardOf(step("s1", { instruction: "Do it." }));
    expect(card.roles).toEqual([]);
    expect(titles(card.sections)).toEqual(["Instruction", "Timing"]);
  });

  it("counts the step's room messages after its runs, and its unanswered questions when it has any", () => {
    const s1 = step("s1", { worker_id: "worker-a" });
    const card = (room: { sent: number; received: number; unanswered: number }) =>
      stepCard(s1, { taskId: "t1", active: true, look: LOOKS.pending, lookOf: (d) => LOOKS[d.status], stepsById: new Map(),
                     room });
    expect(titles(card({ sent: 2, received: 1, unanswered: 0 }).sections)).toEqual(["Runs", "Room", "Timing"]);
    expect(section(card({ sent: 2, received: 1, unanswered: 0 }).sections, "Room")).toEqual({ kind: "facts", title: "Room", facts: [
      { label: "Messages", value: "2 sent, 1 received" },
    ] });
    expect(section(card({ sent: 0, received: 1, unanswered: 1 }).sections, "Room")).toMatchObject({ facts: [
      { label: "Messages", value: "0 sent, 1 received" },
      { label: "Unanswered", value: "1 question" },
    ] });
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
      role_reviews: [{ role: "security", required: true, passed: false, findings: [], summary: "shell injection" }] } }));
    expect(section(byRole.sections, "Review")).toMatchObject({ text: "Review by role security did not pass it:\nshell injection" });
  });

  it("shows no result for a running step, whose result is its previous attempt's", () => {
    const card = cardOf(step("s1", { status: "dispatched", result: { status: "failed", error: "old" } }));
    expect(titles(card.sections)).not.toContain("Result: failed");
  });

  it("says what a step in review waits for", () => {
    const roles = [{ role: "security", required: true, version: 1 }, { role: "perf", required: true, version: 1 }];
    const card = cardOf(step("s1", { status: "completed", review_stage: "role", review_roles: roles, review_position: 1 }));
    expect(section(card.sections, "Runs")).toMatchObject({ facts: [{ label: "Now", value: "Waiting for review: perf" }] });
    const builtin = cardOf(step("s1", { status: "completed", review_stage: "builtin", review_roles: roles }));
    expect(section(builtin.sections, "Runs")).toMatchObject({ facts: [{ label: "Now", value: "Waiting for the built-in review" }] });
  });

  it("gives a dropped step's reason, or the default one", () => {
    const why = "replan at 10:00: the user asked for it. Plan changed; dropped s4";
    expect(section(cardOf(step("s4", { status: "skipped" }), undefined, why).sections, "Skipped")).toMatchObject({ text: why });
    const dropped = cardOf(step("s4", { status: "skipped", completed_at: "2026-10-17T08:05:00Z" }));
    expect(section(dropped.sections, "Skipped")).toMatchObject({ text: "Dropped from the plan by a replan" });
    expect(section(dropped.sections, "Timing")).toMatchObject({ facts: [{ label: "Planned" }, { label: "Dropped" }] });
  });
});

describe("stepSpan", () => {
  const at = (iso: string) => new Date(iso).getTime();
  const span = (extra: Partial<TaskStep>, active = true, now = at("2026-10-17T08:10:00Z")) =>
    stepSpan(step("s1", extra), active, now);
  const START = "2026-10-17T08:00:00Z";
  const EARLIER_FINISH = "2026-10-17T08:05:00Z"; // a step running again keeps its last finish

  it("is how long the step took, until the last time it went to review or ended, retries and reviews included", () => {
    expect(span({ status: "verified", started_at: START, completed_at: "2026-10-17T08:03:12Z" }))
      .toEqual({ label: "took", text: "3m 12s" });
    expect(span({ status: "completed", started_at: START, completed_at: "2026-10-17T08:00:45Z" })) // in review
      .toEqual({ label: "took", text: "45s" });
    expect(span({ status: "failed", started_at: START, completed_at: "2026-10-17T08:07:00Z" }))
      .toEqual({ label: "took", text: "7m 0s" });
    expect(span({ status: "skipped", started_at: START, completed_at: "2026-10-17T08:02:00Z" }))
      .toEqual({ label: "ran for", text: "2m 0s" });
  });

  it("is how long it has run while its attempt runs or awaits a decision, whatever finish it had before", () => {
    expect(span({ status: "dispatched", started_at: "2026-10-17T08:09:20Z" })).toEqual({ label: "running for", text: "40s" });
    expect(span({ status: "deciding", started_at: "2026-10-17T06:00:00Z" })).toEqual({ label: "running for", text: "2h 10m" });
    // A retry after a failed review, and a decision after it: running, not "took" from the earlier finish.
    expect(span({ status: "dispatched", started_at: START, completed_at: EARLIER_FINISH })).toEqual({ label: "running for", text: "10m 0s" });
    expect(span({ status: "deciding", started_at: START, completed_at: EARLIER_FINISH })).toEqual({ label: "running for", text: "10m 0s" });
  });

  it("is nothing for a running step of a task that does not run (stopped, or the server went away)", () => {
    expect(span({ status: "dispatched", started_at: START }, false)).toBeNull();
    expect(span({ status: "deciding", started_at: START, completed_at: EARLIER_FINISH }, false)).toBeNull();
    expect(span({ status: "verified", started_at: START, completed_at: EARLIER_FINISH }, false)).toEqual({ label: "took", text: "5m 0s" });
  });

  it("is nothing before the step started, while it waits to start again, or for a step from before starts were kept", () => {
    expect(span({ status: "pending" })).toBeNull();
    expect(span({ status: "pending", started_at: START, completed_at: EARLIER_FINISH })).toBeNull(); // restarted
    expect(span({ status: "verified", completed_at: "2026-10-17T08:03:00Z" })).toBeNull(); // no start recorded
  });

  it("formats spans for people", () => {
    expect([formatSeconds(0), formatSeconds(59.9), formatSeconds(60), formatSeconds(3599), formatSeconds(3600), formatSeconds(-5)])
      .toEqual(["0s", "59s", "1m 0s", "59m 59s", "1h 0m", "0s"]);
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
    retry_at: null, turns: 0, reminders: 0, opened_at: null, first_reply_at: null, closed_at: null,
    channel_posts: null, ...extra,
  };
}

describe("gateCard", () => {
  const s1 = step("s1", { status: "verified" });
  const g1 = step("g1", { kind: "gate", status: "waiting_user", instruction: "Pick the database", depends_on: ["s1"] });
  const cardOfGate = (g?: UserGate, active = true) =>
    gateCard(g1, g, { look: look("Your turn"), lookOf: (d) => LOOKS[d.status], stepsById: new Map([[s1.id, s1], [g1.id, g1]]),
                      active });

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
    // A stopped task keeps the user's messages for its restart: it is the user's turn, as the gate's panel says.
    expect(section(cardOfGate(gate({ waiting_on: "planner" }), false).sections, "Discussion"))
      .toMatchObject({ facts: [{ label: "Waiting on", value: "You" }] });
  });

  it("shows the step's topic before the gate has a row", () => {
    expect(titles(cardOfGate(undefined).sections)).toEqual(["Topic", "Depends on"]);
  });
});
