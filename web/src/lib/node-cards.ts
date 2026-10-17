/** The details card of each Pipeline node, as data: built from the task here, drawn by
 * `components/pipeline-node-card`. */
import { historyHref } from "@/pages/agent-history";
import { gateWaitsOn } from "@/lib/gates";
import { MERGE_ERRORS, shortCommit, workspaceState } from "@/lib/workspaces";
import {
  reviewingRole,
  type GateAgendaItem,
  type StepWorkspace,
  type TaskDetail,
  type TaskStep,
  type UserGate,
} from "@/lib/api";

/** A node's status as the Pipeline draws it: a word and the node's colors. */
export interface CardLook {
  label: string;
  border: string;
  borderStyle: "solid" | "dashed";
  background: string;
  color: string;
}

export interface CardFact {
  label: string;
  value: string;
  /** An agent's history. */
  href?: string;
}

export type CardSection =
  /** `preview` is the text clipped, when it is long: the card shows it with "Show more". `markdown`: drawn as
   * Markdown, as the Overview draws it; `tone`: the title says something went wrong. */
  | { kind: "text"; title: string; text: string; preview?: string; markdown?: boolean; tone?: "destructive" }
  | { kind: "list"; title: string; items: string[]; mono?: boolean }
  | { kind: "steps"; title: string; steps: { name: string; look: CardLook }[] }
  | { kind: "facts"; title: string; facts: CardFact[] };

/** What a Pipeline node's card shows: its name and status, the roles it runs as, then its sections in order. */
export interface NodeCard {
  name: string;
  look: CardLook;
  roles: string[];
  sections: CardSection[];
}

/** The id of a node's card, which the node names in its aria-details. */
export const nodeCardId = (nodeId: string) => `node-card-${nodeId}`;

/** `text` cut to its first `lines` lines and `chars` characters, with an ellipsis; undefined when it is short. */
export function clipText(text: string, { lines = 4, chars = 320 } = {}): string | undefined {
  const whole = text.trimEnd();
  let cut = whole.split("\n").slice(0, lines).join("\n");
  if (cut.length > chars) cut = cut.slice(0, chars);
  return cut.length < whole.length ? `${cut.trimEnd()}…` : undefined;
}

export function formatWhen(iso: string) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** A span of time for people: 45s, 3m 12s, 2h 5m. */
export function formatSeconds(seconds: number) {
  const sec = Math.max(0, Math.floor(seconds));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor(sec / 60) % 60}m`;
}

function formatSpan(start: string, end: string) {
  return formatSeconds((new Date(end).getTime() - new Date(start).getTime()) / 1000);
}

/** How long a step has run, from the first dispatch of its run, by its status: while it runs or awaits a decision,
 * until `now` ("running for"), and only while its task runs (`active`: a stopped task's step does not run on);
 * once in review or ended, until the last time it went to review or ended ("took", retries and reviews included;
 * "ran for" when it was dropped). Null while it is not started (again), and for a step from before starts were
 * kept. */
export function stepSpan(
  step: Pick<TaskStep, "started_at" | "completed_at" | "status">,
  active: boolean,
  now: number = Date.now(),
): { label: "took" | "ran for" | "running for"; text: string } | null {
  if (!step.started_at) return null;
  if (step.status === "dispatched" || step.status === "deciding") {
    return active ? { label: "running for", text: formatSeconds((now - new Date(step.started_at).getTime()) / 1000) } : null;
  }
  if (step.status === "pending" || !step.completed_at) return null;
  return { label: step.status === "skipped" ? "ran for" : "took", text: formatSpan(step.started_at, step.completed_at) };
}

interface TextOptions {
  clip?: boolean;
  markdown?: boolean;
  tone?: "destructive";
}
const text = (title: string, value: string | null | undefined, { clip, markdown, tone }: TextOptions = {}): CardSection[] =>
  value?.trim()
    ? [{ kind: "text", title, text: value.trim(), ...(clip && { preview: clipText(value.trim()) }), ...(markdown && { markdown }),
         ...(tone && { tone }) }]
    : [];
const list = (title: string, items: string[] | null | undefined, mono = false): CardSection[] =>
  items?.length ? [{ kind: "list", title, items, mono }] : [];
const facts = (title: string, items: (CardFact | false | "" | null | undefined)[]): CardSection[] => {
  const kept = items.filter((item): item is CardFact => !!item);
  return kept.length ? [{ kind: "facts", title, facts: kept }] : [];
};
const str = (value: unknown) => (typeof value === "string" ? value : "");

/** The roles a step runs and is reviewed as, as runs name them: "fe_rd · work v2", "security · review (advisory)". */
function stepRoleLines(step: TaskStep) {
  const version = (v: number | null) => (v != null ? ` v${v}` : "");
  return [
    step.role_key && `${step.role_key} · work${version(step.role_version)}`,
    ...(step.review_roles ?? []).map(
      (item) => `${item.role} · review${version(item.version)}${item.required ? "" : " (advisory)"}`,
    ),
  ].filter((line): line is string => !!line);
}

/** What the step's latest attempt reported, once that attempt ended: the Worker's summary, or why the attempt
 * failed; and the review that did not pass it. A running step's result is its previous attempt's, so none. */
function stepResult(step: TaskStep): CardSection[] {
  const result = step.result;
  if (!result || !["completed", "deciding", "verified", "failed"].includes(step.status)) return [];
  const worked = result.status === "completed";
  const report = worked ? str(result.summary) : str(result.error) || str(result.summary);
  const roleReviews = (Array.isArray(result.role_reviews) ? result.role_reviews : []) as Record<string, unknown>[];
  const reasons = (findings: unknown, summary: unknown) =>
    (Array.isArray(findings) ? findings.map(String).join("\n") : "") || str(summary) || "no reason given";
  const failed = roleReviews
    .filter((r) => r.passed !== true)
    .map((r) => `Review by role ${str(r.role)}${r.required === false ? " (advisory)" : ""} did not pass it:\n${reasons(r.findings, r.summary)}`);
  const review = failed.length
    ? failed.join("\n\n")
    : result.verify_passed === false
      ? `The review did not pass it:\n${reasons(result.verify_findings, result.verify_summary)}`
      : "";
  return [
    ...text(worked ? "Result" : `Result: ${str(result.status) || "unknown"}`, report,
            { clip: true, tone: worked ? undefined : "destructive" }),
    ...text("Review", review, { clip: true, tone: "destructive" }),
  ];
}

/** Where the step worked, when it had a worktree of its own: the latest one's branch, state and start, why it could not
 * be merged and the files in the way, its links, and how many earlier ones it had. */
function stepWorktree(workspaces: StepWorkspace[] | undefined): CardSection[] {
  const latest = workspaces?.[workspaces.length - 1];
  if (!workspaces || !latest) return [];
  return [
    ...facts("Worktree", [
      { label: "Branch", value: latest.branch },
      { label: "State", value: workspaceState(latest) },
      { label: "From", value: `${latest.target_branch} at ${shortCommit(latest.base_commit)}${latest.checkpoint ? " (checkpoint)" : ""}` },
      latest.merge_error && { label: "Why", value: MERGE_ERRORS[latest.merge_error] },
      latest.links.length > 0 && { label: "Linked", value: latest.links.join(", ") },
      workspaces.length > 1 && { label: "Earlier", value: `${workspaces.length - 1} worktree${workspaces.length > 2 ? "s" : ""}` },
    ]),
    ...list("Files in the way of the merge", latest.merge_files, true),
  ];
}

export interface StepCardContext {
  taskId: string;
  /** Whether the step's task runs: a step shows how long it has been running only then. */
  active: boolean;
  look: CardLook;
  /** The look of each step it depends on, by id. */
  lookOf: (step: TaskStep) => CardLook;
  stepsById: Map<string, TaskStep>;
  /** Why a dropped step was dropped. */
  skippedBecause?: string;
  /** The step's worktrees, oldest first, when it had any. */
  workspaces?: StepWorkspace[];
}

/** A step's card: what it does, how it is checked, what it waits for, who ran it and what came of it. */
export function stepCard(
  step: TaskStep, { taskId, active, look, lookOf, stepsById, skippedBecause, workspaces }: StepCardContext,
): NodeCard {
  const deps = (step.depends_on ?? []).map((id) => stepsById.get(id)).filter((s): s is TaskStep => !!s);
  const span = stepSpan(step, active);
  // completed_at is the last time the step went to review or ended; a step running again still has it.
  const finished = step.completed_at && !["pending", "dispatched", "deciding"].includes(step.status) &&
    (step.status === "skipped" ? "Dropped" : "Finished");
  return {
    name: step.step_key || `step ${step.step_index + 1}`,
    look,
    roles: stepRoleLines(step),
    sections: [
      ...text("Instruction", step.instruction),
      ...list("Acceptance criteria", step.acceptance_criteria),
      ...list("Files", step.target_files, true),
      ...(deps.length
        ? [{ kind: "steps" as const, title: "Depends on", steps: deps.map((d) => ({ name: d.step_key || d.id, look: lookOf(d) })) }]
        : []),
      ...facts("Runs", [
        step.worker_id && { label: "Worker", value: step.worker_id, href: historyHref(step.worker_id, taskId) },
        step.reviewer_id && { label: "Reviewer", value: step.reviewer_id, href: historyHref(step.reviewer_id, taskId) },
        step.retries > 0 && { label: "Retries", value: String(step.retries) },
        step.review && { label: "Review", value: "Needs a passing review" },
        step.status === "completed" && step.review_stage && {
          label: "Now",
          value: `Waiting for ${reviewingRole(step) ? `review: ${reviewingRole(step)?.role}` : "the built-in review"}`,
        },
      ]),
      ...stepWorktree(workspaces),
      ...stepResult(step),
      ...(step.status === "skipped" ? text("Skipped", skippedBecause || "Dropped from the plan by a replan") : []),
      ...facts("Timing", [
        { label: "Planned", value: formatWhen(step.created_at) },
        step.started_at && { label: "Started", value: formatWhen(step.started_at) },
        finished && { label: finished, value: formatWhen(step.completed_at!) },
        span && { label: span.label === "took" ? "Took" : span.label === "ran for" ? "Ran for" : "Running for", value: span.text },
      ]),
    ],
  };
}

const AGENDA_STATUS: Record<GateAgendaItem["status"], string> = { open: "Open", resolved: "Resolved", dropped: "Dropped" };

/** A user gate's card: its topic, its questions with each one's state, whom it waits on (the Planner only while its
 * task runs, `active`, as its panel says), what the Planner found and
 * what it settled or why it was cancelled, the steps it waits for, and when it opened and closed. `gate` is absent
 * before the gate has a row. */
export function gateCard(
  step: TaskStep,
  gate: UserGate | undefined,
  { look, lookOf, stepsById, active }: Omit<StepCardContext, "taskId" | "skippedBecause"> & { active: boolean },
): NodeCard {
  const waitsOn = gate && gateWaitsOn(gate, active);
  const deps = (step.depends_on ?? []).map((id) => stepsById.get(id)).filter((s): s is TaskStep => !!s);
  const notDelivered = gate?.not_delivered ?? 0;
  return {
    name: step.step_key || `step ${step.step_index + 1}`,
    look,
    roles: [],
    sections: [
      ...text("Topic", gate?.topic || step.instruction),
      ...list("Agenda", gate?.agenda.map((item) =>
        `${AGENDA_STATUS[item.status]}: ${item.question}${item.status === "resolved" && item.answer ? ` → ${item.answer}` : ""}`)),
      ...facts("Discussion", [
        waitsOn && { label: "Waiting on", value: waitsOn === "user" ? "You" : "The Planner" },
        notDelivered > 0 && { label: "Not delivered", value: `${notDelivered} message${notDelivered === 1 ? "" : "s"}` },
        !!gate?.turns && { label: "Turns", value: String(gate.turns) },
      ]),
      ...text("Findings", gate?.findings, { clip: true }),
      ...list("Decisions", gate?.summary?.decisions),
      ...list("Assumptions", gate?.summary?.assumptions),
      ...list("Still open", gate?.summary?.open),
      ...(gate?.state === "cancelled" ? text("Cancelled", gate.resolution || "Cancelled by the Planner") : []),
      ...(deps.length
        ? [{ kind: "steps" as const, title: "Depends on", steps: deps.map((d) => ({ name: d.step_key || d.id, look: lookOf(d) })) }]
        : []),
      ...facts("Timing", [
        gate?.opened_at && { label: "Opened", value: formatWhen(gate.opened_at) },
        gate?.closed_at && { label: "Closed", value: formatWhen(gate.closed_at) },
      ]),
    ],
  };
}

/** The Planner's card: its agent, the plan's size and its latest progress note. */
export function plannerCard(task: TaskDetail, look: CardLook): NodeCard {
  const note = task.progress?.[0];
  const steps = task.steps ?? [];
  const dropped = steps.filter((s) => s.status === "skipped").length;
  const planned = steps.length - dropped;
  return {
    name: "Planner",
    look,
    roles: [],
    sections: [
      ...facts("Runs", [
        task.planner_id && { label: "Planner", value: task.planner_id, href: historyHref(task.planner_id, task.task_id) },
        steps.length > 0 && {
          label: "Plan",
          value: `${planned} step${planned === 1 ? "" : "s"}${dropped ? `, ${dropped} dropped` : ""}`,
        },
      ]),
      ...(note
        ? text(`Latest note · ${formatWhen(note.at)}`, [note.now && `Now: ${note.now}`, note.next && `Next: ${note.next}`]
            .filter(Boolean).join("\n"), { clip: true })
        : []),
    ],
  };
}

/** The result's card: the task's summary, or its error, and how long it ran. */
export function resultCard(task: TaskDetail, look: CardLook): NodeCard {
  return {
    name: "Result",
    look,
    roles: [],
    sections: [
      ...text("Summary", task.result_summary, { clip: true, markdown: true }),
      ...text("Error", task.error, { clip: true, markdown: true, tone: "destructive" }),
      ...facts("Timing", [
        { label: "Created", value: formatWhen(task.created_at) },
        task.completed_at && { label: "Ended", value: formatWhen(task.completed_at) },
        task.completed_at && { label: "Took", value: formatSpan(task.created_at, task.completed_at) },
      ]),
    ],
  };
}
