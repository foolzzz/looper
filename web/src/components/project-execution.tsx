import type { Project, ProjectExecution, ProjectUpdate } from "@/lib/api";
import { Input } from "@/components/ui/input";

/** The execution settings as the editor holds them: text, "" = inherit. */
export type ExecutionDraft = Record<keyof ProjectExecution, string>;

const FIELDS: (keyof ProjectExecution)[] = [
  "task_budget_usd",
  "task_budget_action",
  "supervise_interval_seconds",
  "review_policy",
  "distill_lessons",
];
const NUMBERS = new Set<keyof ProjectExecution>(["task_budget_usd", "supervise_interval_seconds"]);
// A switch: "true" is on; "" (null) and "false" are off.
const SWITCHES = new Set<keyof ProjectExecution>(["distill_lessons"]);

export function executionDraft(project: Project): ExecutionDraft {
  return Object.fromEntries(FIELDS.map((f) => [f, project[f] == null ? "" : String(project[f])])) as ExecutionDraft;
}

/** The settings that changed, as the API takes them: "" is null; a number that does not parse is sent as
 * typed, so the server's 400 explains it. */
export function executionChanges(draft: ExecutionDraft, project: Project): ProjectUpdate {
  const before = executionDraft(project);
  const changes: ProjectUpdate = {};
  for (const field of FIELDS) {
    const value = draft[field].trim();
    if (value === before[field]) continue;
    const number = Number(value);
    (changes as Record<string, unknown>)[field] = SWITCHES.has(field)
      ? value === "true" || null
      : value === ""
        ? null
        : NUMBERS.has(field) && Number.isFinite(number)
          ? number
          : value;
  }
  return changes;
}

const SELECT =
  "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground";

/** A project's execution settings in its editor; an empty field inherits. */
export function ProjectExecutionFields({
  idPrefix,
  draft,
  onChange,
}: {
  idPrefix: string;
  draft: ExecutionDraft;
  onChange: (draft: ExecutionDraft) => void;
}) {
  const set = (field: keyof ProjectExecution) => (e: { target: { value: string } }) =>
    onChange({ ...draft, [field]: e.target.value });
  const label = (field: keyof ProjectExecution, text: string) => (
    <label htmlFor={`${idPrefix}-${field}`} className="text-xs font-medium text-muted-foreground">
      {text}
    </label>
  );
  return (
    <fieldset className="space-y-3 rounded-md border border-border px-3 py-3">
      <legend className="px-1 text-xs font-medium text-muted-foreground">Execution</legend>
      <p className="text-xs text-muted-foreground">
        How this project&apos;s Planner, Workers and Reviewers run. Empty fields inherit; changes apply to the next run.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          {label("review_policy", "Review")}
          <select id={`${idPrefix}-review_policy`} value={draft.review_policy} onChange={set("review_policy")} className={SELECT}>
            <option value="">Inherit: the Planner decides</option>
            <option value="planner">The Planner decides</option>
            <option value="all">Every step goes to the Reviewer</option>
          </select>
        </div>
        <div className="space-y-1">
          {label("task_budget_usd", "Spend cap per task (USD)")}
          <Input
            id={`${idPrefix}-task_budget_usd`}
            inputMode="decimal"
            value={draft.task_budget_usd}
            onChange={set("task_budget_usd")}
            placeholder="No cap"
          />
        </div>
        <div className="space-y-1">
          {label("task_budget_action", "At the task cap")}
          <select
            id={`${idPrefix}-task_budget_action`}
            value={draft.task_budget_action}
            onChange={set("task_budget_action")}
            className={SELECT}
          >
            <option value="">Inherit: fail the task at once</option>
            <option value="fail">Fail the task at once</option>
            <option value="wrap_up">Let the Planner wrap up, then fail</option>
          </select>
        </div>
        <div className="space-y-1">
          {label("supervise_interval_seconds", "Planner supervision interval (s)")}
          <Input
            id={`${idPrefix}-supervise_interval_seconds`}
            inputMode="numeric"
            value={draft.supervise_interval_seconds}
            onChange={set("supervise_interval_seconds")}
            placeholder="Inherit (0 = off)"
          />
        </div>
      </div>
      <label htmlFor={`${idPrefix}-distill_lessons`} className="flex items-start gap-2 text-sm">
        <input
          id={`${idPrefix}-distill_lessons`}
          type="checkbox"
          className="mt-0.5 h-4 w-4 accent-primary"
          checked={draft.distill_lessons === "true"}
          onChange={(e) => onChange({ ...draft, distill_lessons: e.target.checked ? "true" : "" })}
        />
        <span>
          Distill lessons into knowledge
          <span className="block text-xs text-muted-foreground">
            When a task completes, its Planner turns the task&apos;s findings into a lessons document in this
            project&apos;s knowledge. Off by default.
          </span>
        </span>
      </label>
    </fieldset>
  );
}
