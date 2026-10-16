import { useId, useState } from "react";
import { Bot, CheckCircle2, Loader2 } from "lucide-react";
import { AgentProfileSelect } from "@/components/agent-profiles";
import { SELECT } from "@/components/registry-ui";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  setTaskAgents,
  type EffectiveProfile,
  type SettingSource,
  type Sourced,
  type TaskAgents,
  type TaskAgentsUpdate,
  type TaskLimitRole,
  type TaskRole,
  type TaskRoleConcurrency,
} from "@/lib/api";
import { roleLabel } from "@/lib/runtime-catalog";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { profileName } from "@/lib/use-registry";
import { cn } from "@/lib/utils";

/** Where a role's Agent Profile comes from, in words (as the settings layers name themselves). */
const PROFILE_SOURCES: Partial<Record<SettingSource, string>> = {
  builtin: "built-in default",
  system: "the shared defaults (Settings → Roles)",
  role: "the role (Settings → Roles)",
  project: "this project",
  role_project: "this role in the project",
};

/** Where a project's limit comes from, in words. */
const PROJECT_SOURCES: Partial<Record<SettingSource, string>> = {
  builtin: "built-in default",
  role: "the role in Settings → Roles",
  role_project: "set in the project",
};

const projectLimit = (limit: Sourced<number>) => `the project's limit (${PROJECT_SOURCES[limit.source] ?? limit.source})`;

/** Why this limit applies to the task: its own, the project's, or the project's lower one over the task's own. */
function limitText(limit: TaskRoleConcurrency) {
  if (limit.source === "task") return "this task's own limit";
  if (limit.task != null) return `${projectLimit(limit.project)} applies: the task's own ${limit.task} is higher`;
  return projectLimit(limit.project);
}

const profileText = (profile: Sourced<EffectiveProfile>) =>
  profile.source === "task" ? "chosen for this task" : `from ${PROFILE_SOURCES[profile.source] ?? profile.source}`;

/** A number field's value as the API takes it: "" is null; a value that does not parse goes as typed. */
function numberOrTyped(text: string) {
  const number = Number(text);
  return text === "" ? null : Number.isFinite(number) ? number : text;
}

const LIMIT_LABELS: Record<TaskLimitRole, string> = {
  worker: "Workers at once in this task",
  reviewer: "Reviewers at once in this task",
};
const ROLES: TaskRole[] = ["planner", "worker", "reviewer"];
const LABEL = "block text-xs font-medium text-muted-foreground";
const LINK =
  "text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

function Now({ id, children }: { id?: string; children: React.ReactNode }) {
  return (
    <p id={id} aria-live="polite" className="text-xs text-muted-foreground">
      Now: {children}
    </p>
  );
}

const Value = ({ children }: { children: React.ReactNode }) => (
  <span className="font-mono text-sm font-medium text-foreground">{children}</span>
);

/** Save one change of the task's agents; `error` holds the server's reason when it refuses. */
function useSave(taskId: string, onSaved: (agents: TaskAgents) => void) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const save = async (changes: TaskAgentsUpdate) => {
    setBusy(true);
    setError("");
    try {
      const saved = await setTaskAgents(taskId, changes);
      onSaved(saved.agents);
      return saved;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return null;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, save };
}

function ErrorLine({ id, error }: { id: string; error: string }) {
  return error ? (
    <p id={id} role="alert" className="text-xs text-destructive">
      {error}
    </p>
  ) : null;
}

/** The role's Agent Profile: what runs it and where that comes from; a task that has not ended chooses its own. */
function ProfileField({
  taskId,
  role,
  profile,
  editable,
  onSaved,
}: {
  taskId: string;
  role: TaskRole;
  profile: Sourced<EffectiveProfile>;
  editable: boolean;
  onSaved: (agents: TaskAgents) => void;
}) {
  const id = useId();
  const own = profile.source === "task" ? profile.value.profile_id : "";
  // The choice being saved, shown until the answer.
  const [pending, setPending] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const { busy, error, save } = useSave(taskId, onSaved);

  const choose = async (profileId: string) => {
    if (profileId === own) return;
    setPending(profileId);
    setNote("");
    const saved = await save({ profiles: { [role]: profileId || null } });
    setPending(null);
    if (saved?.note) setNote(saved.note);
  };

  const now = (
    <Now id={`${id}-now`}>
      <Value>{profileName(profile.value)}</Value> · {profileText(profile)}
    </Now>
  );
  if (!editable) {
    return (
      <div className="space-y-1">
        <p className={LABEL}>{AGENT_PROFILE.name}</p>
        {now}
      </div>
    );
  }
  return (
    <div className="min-w-0 space-y-1">
      <label htmlFor={`${id}-profile`} className={LABEL}>
        {AGENT_PROFILE.name}
      </label>
      {now}
      {/* A disabled fieldset disables the picker while a choice saves. */}
      <fieldset disabled={busy} className="min-w-0">
        <AgentProfileSelect
          id={`${id}-profile`}
          value={pending ?? own}
          onChange={choose}
          emptyLabel={own ? "From settings" : `From settings: ${profileName(profile.value)}`}
          roles={[role]}
          describedBy={`${id}-now${error ? ` ${id}-error` : ""}`}
          unlistedName={own ? profileName(profile.value) : undefined}
          className={cn(SELECT, "h-11 sm:h-9")}
        />
      </fieldset>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        {busy && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" aria-label="Saving" />}
        {own && !busy && (
          <button type="button" onClick={() => choose("")} className={cn("min-h-11 text-xs sm:min-h-0", LINK)}>
            Clear the task's choice
          </button>
        )}
      </div>
      <ErrorLine id={`${id}-error`} error={error} />
      {note && !error && (
        <p role="status" className="flex items-start gap-1 text-xs text-success">
          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>Saved: {note}.</span>
        </p>
      )}
    </div>
  );
}

/** A Worker's or Reviewer's limit in the task: what applies and why; a task that has not ended sets its own. */
function LimitField({
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
  onSaved: (agents: TaskAgents) => void;
}) {
  const id = useId();
  const own = limit.task == null ? "" : String(limit.task);
  // null: untouched, the field shows the task's own limit as saved.
  const [draft, setDraft] = useState<string | null>(null);
  const { busy, error, save } = useSave(taskId, onSaved);
  const text = draft ?? own;
  const changed = text.trim() !== own;

  const submit = async (value: number | string | null) => {
    if (await save({ concurrency: { [role]: value } })) setDraft(null);
  };

  const now = (
    <Now id={`${id}-now`}>
      <Value>{limit.value}</Value> · {limitText(limit)}
    </Now>
  );
  if (!editable) {
    return (
      <div className="space-y-1">
        <p className={LABEL}>{LIMIT_LABELS[role]}</p>
        {now}
      </div>
    );
  }
  return (
    <form
      className="space-y-1"
      onSubmit={(e) => {
        e.preventDefault();
        if (changed && !busy) submit(numberOrTyped(text.trim()));
      }}
    >
      <label htmlFor={`${id}-input`} className={LABEL}>
        {LIMIT_LABELS[role]}
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
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => submit(null)} className="h-11 sm:h-8">
            Clear
          </Button>
        )}
      </div>
      <ErrorLine id={`${id}-error`} error={error} />
    </form>
  );
}

/**
 * The task's agents, one row per role: the Agent Profile that runs it and how many run at once, each with where it
 * comes from. A task that has not ended chooses its own profile per role and its own Worker and Reviewer limits,
 * within the project's; the Planner's limit (how many of the project's tasks run at once) is the project's.
 */
export function TaskAgentsSection({
  taskId,
  projectId,
  agents,
  onSaved,
}: {
  taskId: string;
  projectId: string;
  agents: TaskAgents;
  onSaved: (agents: TaskAgents) => void;
}) {
  const hintId = useId();
  const { roles, editable } = agents;
  return (
    <section aria-labelledby={`${hintId}-title`}>
      <h3 id={`${hintId}-title`} className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
        <Bot className="h-4 w-4" aria-hidden="true" />
        Agents
      </h3>
      <div className="divide-y divide-border rounded-lg border border-border bg-card">
        {ROLES.map((role) => (
          <div key={role} className="space-y-2 px-4 py-3">
            <h4 className="text-sm font-medium">{roleLabel(role)}</h4>
            <div className="grid gap-4 sm:grid-cols-2">
              <ProfileField
                taskId={taskId}
                role={role}
                profile={roles[role].profile}
                editable={editable}
                onSaved={onSaved}
              />
              {role === "planner" ? (
                <div className="space-y-1">
                  <p className={LABEL}>Tasks at once in this project</p>
                  <Now>
                    <Value>{roles.planner.concurrency.value}</Value> · {projectLimit(roles.planner.concurrency)}
                  </Now>
                  <p className="text-[11px] text-muted-foreground">
                    A task cannot set it; change it in the project's{" "}
                    <a href={`#projects/${encodeURIComponent(projectId)}/${AGENT_PROFILE.slug}/planner`} className={LINK}>
                      {AGENT_PROFILE.plural} → Planner
                    </a>
                    .
                  </p>
                </div>
              ) : (
                <LimitField
                  taskId={taskId}
                  role={role}
                  limit={roles[role].concurrency}
                  editable={editable}
                  hintId={hintId}
                  onSaved={onSaved}
                />
              )}
            </div>
          </div>
        ))}
        <p id={hintId} className="px-4 py-3 text-[11px] text-muted-foreground">
          {editable
            ? `A chosen ${AGENT_PROFILE.name} runs that role for this task only. Limits: 1–${agents.concurrency_max}. Within the project's limit; lowering it stops nothing already running.`
            : `The task has ended: its ${AGENT_PROFILE.plural} and limits are shown, not changed.`}
        </p>
      </div>
    </section>
  );
}
