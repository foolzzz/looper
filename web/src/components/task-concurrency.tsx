import { useId, useState } from "react";
import { Gauge, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  setTaskConcurrency,
  type SettingSource,
  type Sourced,
  type TaskConcurrency,
  type TaskLimitRole,
  type TaskRoleConcurrency,
} from "@/lib/api";
import { AGENT_PROFILE } from "@/lib/settings-pages";

/** Where a project's limit comes from, in words. */
const PROJECT_SOURCES: Partial<Record<SettingSource, string>> = {
  builtin: "built-in default",
  role: "the role in Settings → Roles",
  role_project: "set in the project",
};

const projectLimit = (limit: Sourced<number>) => `the project's limit (${PROJECT_SOURCES[limit.source] ?? limit.source})`;

/** Why this limit applies to the task: its own, the project's, or the project's lower one over the task's own. */
function appliesText(limit: TaskRoleConcurrency) {
  if (limit.source === "task") return "this task's own limit";
  if (limit.task != null) return `${projectLimit(limit.project)} applies: the task's own ${limit.task} is higher`;
  return projectLimit(limit.project);
}

/** A number field's value as the API takes it: "" is null; a value that does not parse goes as typed. */
function numberOrTyped(text: string) {
  const number = Number(text);
  return text === "" ? null : Number.isFinite(number) ? number : text;
}

const LABELS: Record<TaskLimitRole, string> = {
  worker: "Workers at once in this task",
  reviewer: "Reviewers at once in this task",
};

function Now({ id, value, children }: { id?: string; value: number; children: React.ReactNode }) {
  return (
    <p id={id} aria-live="polite" className="text-xs text-muted-foreground">
      Now: <span className="font-mono text-sm font-medium text-foreground">{value}</span> · {children}
    </p>
  );
}

function RoleLimit({
  taskId,
  role,
  limit,
  editable,
  hintId,
  onSaved,
}: {
  taskId: string;
  role: TaskLimitRole;
  limit: TaskRoleConcurrency;
  editable: boolean;
  hintId: string;
  onSaved: (concurrency: TaskConcurrency) => void;
}) {
  const id = useId();
  const own = limit.task == null ? "" : String(limit.task);
  // null: untouched, the field shows the task's own limit as saved.
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const text = draft ?? own;
  const changed = text.trim() !== own;

  const save = async (value: number | string | null) => {
    setBusy(true);
    setError("");
    try {
      onSaved(await setTaskConcurrency(taskId, { [role]: value }));
      setDraft(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const now = (
    <Now id={`${id}-now`} value={limit.value}>
      {appliesText(limit)}
    </Now>
  );
  if (!editable) {
    return (
      <div className="space-y-1">
        <p className="text-xs font-medium text-muted-foreground">{LABELS[role]}</p>
        {now}
      </div>
    );
  }
  return (
    <form
      className="space-y-1"
      onSubmit={(e) => {
        e.preventDefault();
        if (changed && !busy) save(numberOrTyped(text.trim()));
      }}
    >
      <label htmlFor={`${id}-input`} className="block text-xs font-medium text-muted-foreground">
        {LABELS[role]}
      </label>
      {now}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          id={`${id}-input`}
          inputMode="numeric"
          value={text}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={`Project's: ${limit.project.value}`}
          aria-invalid={!!error}
          aria-describedby={`${id}-now ${hintId}${error ? ` ${id}-error` : ""}`}
          className="h-11 w-36 sm:h-9"
        />
        <Button type="submit" size="sm" disabled={!changed || busy} className="h-11 sm:h-8">
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          Save
        </Button>
        {limit.task != null && (
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => save(null)} className="h-11 sm:h-8">
            Clear
          </Button>
        )}
      </div>
      {error && (
        <p id={`${id}-error`} role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}

/**
 * How many of a task's roles run at once: the project's task limit (read-only here), and the Worker and Reviewer
 * limits that apply to the task, each with where it comes from. A task that has not ended sets its own Worker and
 * Reviewer limits, within the project's.
 */
export function TaskConcurrencySection({
  taskId,
  projectId,
  concurrency,
  onSaved,
}: {
  taskId: string;
  projectId: string;
  concurrency: TaskConcurrency;
  onSaved: (concurrency: TaskConcurrency) => void;
}) {
  const hintId = useId();
  const { planner } = concurrency.roles;
  return (
    <section aria-labelledby={`${hintId}-title`}>
      <h3 id={`${hintId}-title`} className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
        <Gauge className="h-4 w-4" aria-hidden="true" />
        Concurrency
      </h3>
      <div className="space-y-4 rounded-lg border border-border bg-card p-4">
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="space-y-1">
            <p className="text-xs font-medium text-muted-foreground">Tasks at once in this project</p>
            <Now value={planner.value}>{projectLimit(planner)}</Now>
            <p className="text-[11px] text-muted-foreground">
              A task cannot set it; change it in the project's{" "}
              <a
                href={`#projects/${encodeURIComponent(projectId)}/${AGENT_PROFILE.slug}/planner`}
                className="text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {AGENT_PROFILE.plural} → Planner
              </a>
              .
            </p>
          </div>
          {(["worker", "reviewer"] as const).map((role) => (
            <RoleLimit
              key={role}
              taskId={taskId}
              role={role}
              limit={concurrency.roles[role]}
              editable={concurrency.editable}
              hintId={hintId}
              onSaved={onSaved}
            />
          ))}
        </div>
        <p id={hintId} className="text-[11px] text-muted-foreground">
          {concurrency.editable
            ? `1–${concurrency.max}. Within the project's limit; lowering it stops nothing already running.`
            : "The task has ended: its limits are shown, not changed."}
        </p>
      </div>
    </section>
  );
}
