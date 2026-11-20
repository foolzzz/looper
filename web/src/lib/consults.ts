/** The Planner's consults of the advisor as the task page shows them: each one's label and state, where the
 * Pipeline draws it, its card, the steps whose checkpoint consult did not finish, and the runs under the Planner
 * run that started them. Never a request's bindings, nor what a check matched (the server sends neither). */
import type { AdvisorConsult, LlmRecord } from "@/lib/api";
import { clipText, formatSeconds, formatWhen, type CardFact, type CardLook, type CardSection, type NodeCard } from "@/lib/node-cards";
import { formatUsd } from "@/lib/usage";

const LIVE: ReadonlySet<AdvisorConsult["state"]> = new Set(["drafting", "linting", "asking", "needs_info"]);

/** Whether the consult is still going: the Planner writing or answering, its request checked, or the advisor asked. */
export const consultLive = (consult: AdvisorConsult) => LIVE.has(consult.state);

/** A request's kind, as a node names it. */
const KIND_LABEL: Record<string, string> = {
  diagnose: "诊断", design_choice: "方案选择", plan_review: "计划评审", unblock: "解阻",
};

/** A request's kind, as a card names it. */
const KIND_TEXT: Record<string, string> = {
  diagnose: "Diagnose a failure", design_choice: "Choose between options", plan_review: "Review the plan",
  unblock: "Get unstuck",
};

/** Where a consult came from. */
export const SOURCE_TEXT: Record<string, string> = {
  checkpoint: "Plan review before the step started",
  optional: "Asked by the Planner",
  ladder: "Asked after a repeated failure",
};

/** What a failed check of the request found, by its category (the format, code and judge checks). */
export const CHECK_TEXT: Record<string, string> = {
  format: "not in the request format",
  secret: "a key, token or secret",
  binding: "what an alias stands for",
  local_path: "a local path",
  url: "a URL",
  identity: "an address, person or id",
  person: "a person",
  internal_name: "an internal name",
  raw_log: "a raw log",
  dangling: "a reference to what the request does not contain",
  verbatim: "the local material copied word for word",
  not_standalone: "it did not stand on its own",
  unanswerable: "it was too vague to answer",
  other: "something else private",
};

export const checkText = (category: string) => CHECK_TEXT[category] ?? category.replaceAll("_", " ");

export const ADOPTION_TEXT: Record<string, string> = {
  adopted: "Planner adopted it",
  partial: "Planner adopted part of it",
  rejected: "Planner did not adopt it",
};

/** How a consult ended, in a node's words: still going, answered and what the Planner took of it, or why not. */
export function consultResult(consult: AdvisorConsult): string {
  if (consultLive(consult)) return consult.state === "needs_info" ? "回问中" : "请教中";
  if (consult.state === "answered") {
    if (consult.status === "out_of_scope") return "拒答";
    return consult.adoption ? ADOPTION_LABEL[consult.adoption] : "已回答";
  }
  return ENDED_LABEL[consult.state] ?? consult.state;
}

const ADOPTION_LABEL: Record<NonNullable<AdvisorConsult["adoption"]>, string> = {
  adopted: "已采纳", partial: "部分采纳", rejected: "未采纳",
};
const ENDED_LABEL: Partial<Record<AdvisorConsult["state"], string>> = {
  blocked: "被拦", failed: "失败", over_budget: "超预算",
};

/** A consult node's label: its kind and how it ended, e.g. "诊断 · 已采纳"; a request not written yet has no kind. */
export const consultLabel = (consult: AdvisorConsult) =>
  `${KIND_LABEL[consult.kind ?? ""] ?? "请教"} · ${consultResult(consult)}`;

/** Whether the consult ended with an answer: answered, and not refused (`out_of_scope`). */
export const consultAnswered = (consult: AdvisorConsult) => consult.state === "answered" && consult.status === "answered";

/** A consult's state as a colour says it (the words always say it too): going, answered, or ended without one. */
export type ConsultTone = "live" | "answered" | "failed";

export function consultTone(consult: AdvisorConsult): ConsultTone {
  if (consultLive(consult)) return "live";
  return consultAnswered(consult) ? "answered" : "failed";
}

const byTime = (a: AdvisorConsult, b: AdvisorConsult) => a.created_at.localeCompare(b.created_at) || a.id - b.id;

/** Where the Pipeline draws each consult, in time order: by the plan (a consult while planning, of no step), before
 * its step (a checkpoint) or after it (one the Planner chose, or the ladder's). */
export interface ConsultPlaces {
  plan: AdvisorConsult[];
  before: Map<string, AdvisorConsult[]>;
  after: Map<string, AdvisorConsult[]>;
}

export function consultPlaces(consults: AdvisorConsult[]): ConsultPlaces {
  const places: ConsultPlaces = { plan: [], before: new Map(), after: new Map() };
  for (const consult of [...consults].sort(byTime)) {
    if (!consult.step_id) {
      places.plan.push(consult);
      continue;
    }
    const side = consult.source === "checkpoint" ? places.before : places.after;
    side.set(consult.step_id, [...(side.get(consult.step_id) ?? []), consult]);
  }
  return places;
}

/** The marker of a step whose checkpoint consult ended without an answer (the step went on without it). */
export const CHECKPOINT_UNFINISHED = "检查点请教未完成";

/** The steps whose checkpoint consult ended without an answer (one the advisor refused included). */
export function unfinishedCheckpoints(consults: AdvisorConsult[]): Set<string> {
  return new Set(consults
    .filter((c) => c.step_id && c.source === "checkpoint" && !consultLive(c) && !consultAnswered(c))
    .map((c) => c.step_id!));
}

/** How a consult ended, in a card's words, with why it failed. */
export function stateText(consult: AdvisorConsult): string {
  const why = consult.failure && !["answered", "blocked"].includes(consult.state) ? ` (${consult.failure})` : "";
  return {
    drafting: "The Planner is writing the request",
    linting: "Checking the request",
    asking: "Asking the advisor",
    needs_info: "The advisor asked back",
    answered: consult.status === "out_of_scope" ? "The advisor said it was beyond what it can judge" : "Answered",
    blocked: "Blocked: the request failed its checks",
    failed: "Failed",
    over_budget: "Over its budget",
  }[consult.state] + why;
}

const lines = (title: string, items: string[]): CardSection[] =>
  items.length ? [{ kind: "list", title, items }] : [];
const words = (title: string, text: string | undefined): CardSection[] =>
  text?.trim() ? [{ kind: "text", title, text: text.trim() }] : [];
const kept = (title: string, facts: (CardFact | false | null | undefined)[]): CardSection[] => {
  const shown = facts.filter((fact): fact is CardFact => !!fact);
  return shown.length ? [{ kind: "facts", title, facts: shown }] : [];
};

/** A consult's card: how it came about and ended, the request that was sent, the advisor's questions back and the
 * Planner's answers, the advice, what the Planner took of it and why, its cost and duration. A request that failed
 * its checks shows only their categories and how many times. */
export function consultCard(consult: AdvisorConsult, look: CardLook): NodeCard {
  const ask = consult.question;
  const advice = consult.advice;
  return {
    name: consultLabel(consult),
    look,
    roles: [],
    sections: [
      ...kept("Consult", [
        { label: "From", value: SOURCE_TEXT[consult.source ?? ""] ?? "Unknown" },
        !!consult.kind && { label: "Kind", value: KIND_TEXT[consult.kind] ?? consult.kind },
        consult.attempt != null && { label: "For attempt", value: String(consult.attempt) },
        { label: "State", value: stateText(consult) },
        consult.rounds > 0 && { label: "Rounds", value: String(consult.rounds) },
        consult.returns > 0 && { label: "Sent back", value: `${consult.returns}×` },
      ]),
      ...lines("Checks the request failed", consult.checks.map(({ stage, category, count }) =>
        `${checkText(category)} (${stage} check, ${count}×)`)),
      ...(ask ? [
        ...words("Problem", ask.problem),
        ...words("Goal", ask.goal),
        ...lines("Questions", (ask.questions ?? []).map((q) => `${q.id}: ${q.text}`)),
        ...lines("Options", (ask.options ?? []).map((o) =>
          [`${o.id}: ${o.summary}`, o.pros && `pros: ${o.pros}`, o.cons && `cons: ${o.cons}`].filter(Boolean).join(" · "))),
        ...lines("Acceptance", ask.acceptance ?? []),
        ...lines("Constraints", ask.constraints ?? []),
        ...lines("Stack", ask.stack ?? []),
        ...lines("Tried", (ask.attempts ?? []).map((a) =>
          [a.approach, a.signal && `saw: ${a.signal}`, a.learned && `learned: ${a.learned}`].filter(Boolean).join(" · "))),
        ...(ask.artifacts ?? []).map((a): CardSection => ({ kind: "text", title: `Material: ${a.kind}${a.lang ? ` (${a.lang})` : ""}`,
          text: a.content, preview: clipText(a.content) })),
      ] : []),
      ...lines("The advisor asked back", consult.needs_info.map((q) => `${q.id}: ${q.question}${q.why ? ` (${q.why})` : ""}`)),
      ...lines("The Planner answered", (ask?.prior_rounds ?? []).map((r) => `${r.needs_info_id}: ${r.answer}`)),
      ...(advice?.answers ?? []).flatMap((a) => words(`Answer ${a.question_id} · confidence ${a.confidence}`,
        [a.conclusion, a.basis.length && `Basis: ${a.basis.join("; ")}`,
         a.assumptions.length && `Assumes: ${a.assumptions.join("; ")}`].filter(Boolean).join("\n"))),
      ...lines("On the options", (advice?.options ?? []).map((o) => `${o.id}: ${o.verdict}${o.why ? `: ${o.why}` : ""}`)),
      ...lines("Next steps", (advice?.next_steps ?? []).map((s) => `${s.id}: ${s.action}${s.verify ? ` (verify: ${s.verify})` : ""}`)),
      ...lines("Risks", advice?.risks ?? []),
      ...kept("The Planner's stance", [
        consult.adoption && { label: "Took", value: ADOPTION_TEXT[consult.adoption] },
        consult.unlocked != null && { label: "Attempt", value: consult.unlocked
          ? `${consult.attempt ?? "?"} passed after it` : `${consult.attempt ?? "?"} did not pass` },
      ]),
      ...lines("Why", consult.adoption_items.map((i) => `${i.ref ?? "?"}: ${i.use ?? "?"}${i.why ? `: ${i.why}` : ""}`)),
      ...kept("Cost and time", [
        { label: "Spent", value: formatUsd(consult.spent_usd) },
        { label: "Asked", value: formatWhen(consult.created_at) },
        consult.duration_s != null && { label: "Took", value: formatSeconds(consult.duration_s) },
      ]),
    ],
  };
}

/** A consult's run (the advisor's or its judge's) as the run tree lists it. */
export interface ConsultRun {
  record: LlmRecord;
  kind: "advisor" | "advice_check";
  parentId: number | null;
}

/** The consults' runs under the Planner run that started each, in the order they started; the Planner runs that
 * started none are left out, and a run whose Planner run is not listed comes last, under none. */
export function consultRunTree(planner: LlmRecord[], runs: ConsultRun[]): { parent: LlmRecord | null; runs: ConsultRun[] }[] {
  const ids = new Set(planner.map((r) => r.id));
  const order = (a: ConsultRun, b: ConsultRun) => a.record.created_at.localeCompare(b.record.created_at) || a.record.id - b.record.id;
  const tree = planner
    .map((parent) => ({ parent: parent as LlmRecord | null, runs: runs.filter((r) => r.parentId === parent.id).sort(order) }))
    .filter((branch) => branch.runs.length);
  const orphans = runs.filter((r) => r.parentId == null || !ids.has(r.parentId)).sort(order);
  return orphans.length ? [...tree, { parent: null, runs: orphans }] : tree;
}
