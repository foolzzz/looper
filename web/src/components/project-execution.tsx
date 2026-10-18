import type { Project, ProjectExecution, ProjectUpdate } from "@/lib/api";
import { Input } from "@/components/ui/input";
import { ROOM_LIMITS } from "@/lib/room";

/** The execution settings as the editor holds them: text, "" = inherit; a list is one item per line. */
export type ExecutionDraft = Record<keyof ProjectExecution, string>;

const FIELDS: (keyof ProjectExecution)[] = [
  "task_budget_usd",
  "task_budget_action",
  "supervise_interval_seconds",
  "review_policy",
  "distill_lessons",
  "step_workspaces",
  "workspace_links",
  "max_step_retries",
  "planner_roles",
  "role_text_max_chars",
  "user_gates",
  "gate_reminder_hours",
  "task_workspaces",
  "operator_notes",
  "agent_room",
  ...ROOM_LIMITS.map((limit) => limit.field),
];
const NUMBERS = new Set<keyof ProjectExecution>([
  "task_budget_usd",
  "supervise_interval_seconds",
  "max_step_retries",
  "role_text_max_chars",
  "gate_reminder_hours",
  ...ROOM_LIMITS.map((limit) => limit.field),
]);

const LISTS = new Set<keyof ProjectExecution>(["workspace_links"]);

export function executionDraft(project: Project): ExecutionDraft {
  const text = (value: unknown) => (value == null ? "" : Array.isArray(value) ? value.join("\n") : String(value));
  return Object.fromEntries(FIELDS.map((f) => [f, text(project[f])])) as ExecutionDraft;
}

/** A list field's lines, without blank ones; null when there are none. */
const lines = (value: string) => {
  const items = value.split("\n").map((line) => line.trim()).filter(Boolean);
  return items.length ? items : null;
};

/** The settings that changed, as the API takes them: "" is null; a number that does not parse is sent as
 * typed, so the server's 400 explains it. */
export function executionChanges(draft: ExecutionDraft, project: Project): ProjectUpdate {
  const before = executionDraft(project);
  const changes: ProjectUpdate = {};
  for (const field of FIELDS) {
    const value = draft[field].trim();
    if (value === before[field]) continue;
    const number = Number(value);
    (changes as Record<string, unknown>)[field] = LISTS.has(field)
      ? lines(value)
      : value === "" ? null : NUMBERS.has(field) && Number.isFinite(number) ? number : value;
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
        <div className="space-y-1">
          {label("max_step_retries", "Retries per step")}
          <Input
            id={`${idPrefix}-max_step_retries`}
            inputMode="numeric"
            value={draft.max_step_retries}
            onChange={set("max_step_retries")}
            placeholder="Inherit: 2 (0–20)"
          />
        </div>
        <div className="space-y-1">
          {label("distill_lessons", "Lessons when a task completes")}
          <select
            id={`${idPrefix}-distill_lessons`}
            value={draft.distill_lessons}
            onChange={set("distill_lessons")}
            aria-describedby={`${idPrefix}-distill_lessons-help`}
            className={SELECT}
          >
            <option value="off">Off</option>
            <option value="draft">Save a draft to review in Knowledge</option>
            <option value="write">Write into knowledge</option>
          </select>
          <p id={`${idPrefix}-distill_lessons-help`} className="text-xs text-muted-foreground">
            The Planner turns the task&apos;s findings into a lessons document. Agents see a draft only after you
            confirm it on the Knowledge tab.
          </p>
        </div>
        <div className="space-y-1">
          {label("step_workspaces", "Steps that run at the same time")}
          <select
            id={`${idPrefix}-step_workspaces`}
            value={draft.step_workspaces}
            onChange={set("step_workspaces")}
            aria-describedby={`${idPrefix}-step_workspaces-help`}
            className={SELECT}
          >
            <option value="">Inherit: share the task directory</option>
            <option value="shared">Share the task directory</option>
            <option value="worktree">A git worktree each</option>
          </select>
          <p id={`${idPrefix}-step_workspaces-help`} className="text-xs text-muted-foreground">
            In a git checkout on a branch, each such step works on a branch of its own. Accepted, it is merged into
            the task directory&apos;s branch with a merge commit, after its leftovers are committed.
          </p>
        </div>
        <div className="space-y-1">
          {label("workspace_links", "Linked into Looper's worktrees")}
          <textarea
            id={`${idPrefix}-workspace_links`}
            value={draft.workspace_links}
            onChange={set("workspace_links")}
            aria-describedby={`${idPrefix}-workspace_links-help`}
            placeholder={".venv\n.env"}
            rows={3}
            spellCheck={false}
            className="flex w-full rounded-md border border-input bg-transparent px-3 py-1.5 font-mono text-sm text-foreground"
          />
          <p id={`${idPrefix}-workspace_links-help`} className="text-xs text-muted-foreground">
            One path per line, relative to the directory: ignored local state a worktree would lack. In a task&apos;s
            worktree each is a link to the workspace&apos;s own, in a step&apos;s to the task directory&apos;s; shared,
            and never committed.
          </p>
        </div>
        <div className="space-y-1">
          {label("task_workspaces", "Where tasks run")}
          <select
            id={`${idPrefix}-task_workspaces`}
            value={draft.task_workspaces}
            onChange={set("task_workspaces")}
            aria-describedby={`${idPrefix}-task_workspaces-help`}
            className={SELECT}
          >
            <option value="">Inherit: in the directory itself</option>
            <option value="in_place">In the directory itself</option>
            <option value="worktree">In a git worktree of it</option>
          </select>
          <p id={`${idPrefix}-task_workspaces-help`} className="text-xs text-muted-foreground">
            For tasks that name no directory of their own. In a worktree a task works on a branch of its own, which it
            leaves to you to merge; your checkout is untouched.
          </p>
        </div>
        <div className="space-y-1">
          {label("operator_notes", "Operator's notes")}
          {/* Not set is on; there is no layer to inherit from. */}
          <select
            id={`${idPrefix}-operator_notes`}
            value={draft.operator_notes || "on"}
            onChange={set("operator_notes")}
            aria-describedby={`${idPrefix}-operator_notes-help`}
            className={SELECT}
          >
            <option value="off">Off</option>
            <option value="on">On</option>
          </select>
          <p id={`${idPrefix}-operator_notes-help`} className="text-xs text-muted-foreground">
            On unless you turn it off: the project&apos;s Planners, Workers and Reviewers get a read-only copy of your Claude Code notes
            (its auto memory) for the directory they work in, whichever runtime runs the step. Looper never changes them.
          </p>
        </div>
        <div className="space-y-1">
          {label("planner_roles", "Planner roles")}
          {/* Not set is off; there is no layer to inherit from. */}
          <select
            id={`${idPrefix}-planner_roles`}
            value={draft.planner_roles || "off"}
            onChange={set("planner_roles")}
            aria-describedby={`${idPrefix}-planner_roles-help`}
            className={SELECT}
          >
            <option value="off">Off</option>
            <option value="on">On</option>
          </select>
          <p id={`${idPrefix}-planner_roles-help`} className="text-xs text-muted-foreground">
            When on, the project&apos;s Planners may define roles for their steps: an Agent Profile from the
            project&apos;s allowed list, a role prompt and a harness. A review role adds a review after the built-in
            Reviewer and never replaces it.
          </p>
        </div>
        <div className="space-y-1">
          {label("role_text_max_chars", "Role text limit (characters)")}
          <Input
            id={`${idPrefix}-role_text_max_chars`}
            inputMode="numeric"
            value={draft.role_text_max_chars}
            onChange={set("role_text_max_chars")}
            aria-describedby={`${idPrefix}-role_text_max_chars-help`}
            placeholder="8,000 (500–20,000)"
          />
          <p id={`${idPrefix}-role_text_max_chars-help`} className="text-xs text-muted-foreground">
            The most characters a role&apos;s prompt and its harness may each have.
          </p>
        </div>
        <div className="space-y-1">
          {label("user_gates", "User gates")}
          {/* Not set is on; there is no layer to inherit from. */}
          <select
            id={`${idPrefix}-user_gates`}
            value={draft.user_gates || "on"}
            onChange={set("user_gates")}
            aria-describedby={`${idPrefix}-user_gates-help`}
            className={SELECT}
          >
            <option value="off">Off</option>
            <option value="on">On</option>
          </select>
          <p id={`${idPrefix}-user_gates-help`} className="text-xs text-muted-foreground">
            When on, the project&apos;s Planners may stop part of the plan to ask you what only you can decide; the
            rest of the plan goes on. Tasks a schedule starts never do.
          </p>
        </div>
        <div className="space-y-1">
          {label("gate_reminder_hours", "Gate reminders (hours)")}
          <Input
            id={`${idPrefix}-gate_reminder_hours`}
            inputMode="numeric"
            value={draft.gate_reminder_hours}
            onChange={set("gate_reminder_hours")}
            aria-describedby={`${idPrefix}-gate_reminder_hours-help`}
            placeholder="24 (0 = none, at most 168)"
          />
          <p id={`${idPrefix}-gate_reminder_hours-help`} className="text-xs text-muted-foreground">
            Hours between the reminders of a gate that waits for you, noted in its discussion.
          </p>
        </div>
        <div className="space-y-1">
          {label("agent_room", "Agent room")}
          {/* Not set is on: the one switch here that is on by default. */}
          <select
            id={`${idPrefix}-agent_room`}
            value={draft.agent_room || "on"}
            onChange={set("agent_room")}
            aria-describedby={`${idPrefix}-agent_room-help`}
            className={SELECT}
          >
            <option value="on">On</option>
            <option value="off">Off</option>
          </select>
          <p id={`${idPrefix}-agent_room-help`} className="text-xs text-muted-foreground">
            Lets the task&apos;s agents message each other through Looper&apos;s room tools; on by default.
          </p>
        </div>
        <fieldset className="rounded-md border border-border px-3 py-3 sm:col-span-2">
          <legend className="px-1 text-xs font-medium text-muted-foreground">Agent room limits</legend>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {ROOM_LIMITS.map(({ field, label: text, help, min, max, fallback }) => (
              <div key={field} className="space-y-1">
                {label(field, text)}
                <Input
                  id={`${idPrefix}-${field}`}
                  inputMode={Number.isInteger(min) ? "numeric" : "decimal"}
                  value={draft[field]}
                  onChange={set(field)}
                  aria-describedby={`${idPrefix}-${field}-help`}
                  placeholder={`${fallback.toLocaleString()} (${min.toLocaleString()}–${max.toLocaleString()})`}
                />
                <p id={`${idPrefix}-${field}-help`} className="text-xs text-muted-foreground">
                  {help} {min.toLocaleString()}–{max.toLocaleString()}; default {fallback.toLocaleString()}.
                </p>
              </div>
            ))}
          </div>
        </fieldset>
      </div>
    </fieldset>
  );
}
