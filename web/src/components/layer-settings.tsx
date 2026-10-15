import { useEffect, useState } from "react";
import { CheckCircle2, Loader2, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { HarnessField } from "@/components/harness-field";
import { AgentProfileSelect } from "@/components/agent-profiles";
import type {
  AgentRole,
  EffectiveProfile,
  EffectiveSettings,
  LayerUpdate,
  LayerValues,
  ProjectRole,
  SettingLayer,
  SettingLimits,
  SettingSource,
  Sourced,
} from "@/lib/api";
import { formatUsd } from "@/lib/usage";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { cn } from "@/lib/utils";

/** Where a value came from, in words. */
const LAYER_LABELS: Record<SettingSource, string> = {
  builtin: "built-in default",
  profile: `the ${AGENT_PROFILE.name}`,
  system: "the shared defaults (Settings → Roles)",
  role: "the role (Settings → Roles)",
  project: "this project",
  role_project: "this role in the project",
  task: "this task",
};

/** What a task role's concurrency limit counts, in one project. */
const CONCURRENCY_LABELS: Record<ProjectRole, string> = {
  planner: "Tasks running at once per project",
  worker: "Workers running at once per project",
  reviewer: "Reviewers running at once per project",
};

const ENV_NAME = /^[A-Z_][A-Z0-9_]*$/;
const CREDENTIAL_WORDS = ["KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"];

/** Why an environment variable name would be refused, or "" when it is fine (the server checks again). */
function envNameProblem(key: string) {
  if (!key) return "A name is required.";
  if (!ENV_NAME.test(key)) return "Use capitals, digits and _ (not starting with a digit).";
  if (CREDENTIAL_WORDS.some((word) => key.includes(word))) return "Looks like a credential: secrets do not belong here.";
  return "";
}

interface EnvRow {
  key: string;
  value: string;
  /** This layer unsets the variable (not in Settings). */
  unset: boolean;
}

/** A layer's values as the editor holds them: text, "" = not set here. */
interface Draft {
  harness: string;
  /** An Agent Profile's profile_id. */
  profile: string;
  effort: string;
  env: EnvRow[];
  budget: string;
  concurrency: string;
}

function draftOf(values: LayerValues): Draft {
  return {
    harness: values.harness_prompt ?? "",
    profile: values.profile_id ?? "",
    effort: values.effort ?? "",
    env: values.env.map((e) => ({ key: e.key, value: e.value ?? "", unset: e.value === null })),
    budget: values.run_budget_usd == null ? "" : String(values.run_budget_usd),
    concurrency: values.max_concurrency == null ? "" : String(values.max_concurrency),
  };
}

/** A number field's change as the API takes it: "" is null; a value that does not parse goes as typed. */
function numberOrTyped(text: string) {
  const number = Number(text);
  return text === "" ? null : Number.isFinite(number) ? number : text;
}

/** The fields that changed, as the API takes them: "" is null; a cap that does not parse goes as typed. */
function changesOf(draft: Draft, values: LayerValues): LayerUpdate {
  const before = draftOf(values);
  const changes: LayerUpdate = {};
  if (draft.harness !== before.harness) changes.harness_prompt = draft.harness.trim() ? draft.harness : null;
  if (draft.profile !== before.profile) changes.profile_id = draft.profile || null;
  if (draft.effort !== before.effort) changes.effort = draft.effort || null;
  if (JSON.stringify(draft.env) !== JSON.stringify(before.env))
    changes.env = draft.env.map((e) => ({ key: e.key, value: e.unset ? null : e.value }));
  const budget = draft.budget.trim();
  if (budget !== before.budget) changes.run_budget_usd = numberOrTyped(budget);
  const concurrency = draft.concurrency.trim();
  if (concurrency !== before.concurrency) changes.max_concurrency = numberOrTyped(concurrency);
  return changes;
}

/** "set here", "from Settings", "the Worker's in this project"… */
function sourceText(source: SettingSource, layer: SettingLayer, fromRole?: string) {
  if (fromRole) {
    const whose = `the ${fromRole[0].toUpperCase()}${fromRole.slice(1)}'s`;
    return source === "role_project" ? `${whose} in this project` : `${whose} in Settings → Roles`;
  }
  return source === layer ? "set here" : `from ${LAYER_LABELS[source]}`;
}

/** What an empty harness at each layer inherits. */
const HARNESS_INHERITS: Record<Exclude<SettingLayer, "builtin">, string> = {
  system: "the built-in harness",
  role: "the shared defaults",
  project: "Settings → Roles",
  role_project: "the project or Settings → Roles",
};

function Now({ id, children }: { id?: string; children: React.ReactNode }) {
  return (
    <p id={id} className="text-[11px] text-muted-foreground">
      {children}
    </p>
  );
}

function NowValue<T>({
  id,
  setting,
  layer,
  show,
}: {
  id: string;
  setting: Sourced<T> | undefined;
  layer: SettingLayer;
  show: (value: T) => string;
}) {
  if (!setting) return null;
  return (
    <Now id={id}>
      Now: <span className="font-mono text-foreground">{show(setting.value)}</span> · {sourceText(setting.source, layer)}
    </Now>
  );
}

const SELECT = "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground";

/**
 * One settings layer's harness, Agent Profile, effort, environment and per-run cap; a Planner's, Worker's or
 * Reviewer's role layer (in Settings → Roles or in a project) also has the role's concurrency limit per project. An
 * empty field is not set here and inherits; with `effective`, each field also says what applies now and where it
 * comes from. `profileFirst` puts the Agent Profile above the other fields, which follow as "More overrides".
 */
export function LayerSettingsForm({
  idPrefix,
  layer,
  values,
  effective,
  limits,
  onSave,
  harnessHint,
  roles,
  submitLabel = "Save",
  onDirtyChange,
  profileFirst = false,
}: {
  idPrefix: string;
  layer: Exclude<SettingLayer, "builtin">;
  values: LayerValues;
  /** What applies to the agent this layer is for; omitted where it differs by role (a project's layer). */
  effective?: EffectiveSettings;
  limits: SettingLimits;
  /** Store the changes; resolves to this layer's values as saved. */
  onSave: (changes: LayerUpdate) => Promise<LayerValues>;
  harnessHint?: React.ReactNode;
  /** The roles this layer selects an Agent Profile for; one that cannot run them all is disabled. */
  roles: AgentRole[];
  submitLabel?: string;
  /** Told whether the form holds unsaved edits. */
  onDirtyChange?: (dirty: boolean) => void;
  profileFirst?: boolean;
}) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(values));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const isSystem = layer === "system";
  const [only] = roles;
  const concurrencyRole: ProjectRole | null =
    (layer === "role" || layer === "role_project") && roles.length === 1 && only !== "manager" ? only : null;

  const set = (fields: Partial<Draft>) => {
    setDraft((d) => ({ ...d, ...fields }));
    setSaved(false);
  };
  const setEntry = (index: number, entry: Partial<EnvRow>) =>
    set({ env: draft.env.map((current, i) => (i === index ? { ...current, ...entry } : current)) });

  const changes = changesOf(draft, values);
  const dirty = Object.keys(changes).length > 0;
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);
  const envProblems = draft.env.map((entry) => envNameProblem(entry.key));
  const invalid = draft.harness.length > limits.harness_max_chars || envProblems.some(Boolean);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!dirty || invalid) return;
    setBusy(true);
    setError("");
    try {
      setDraft(draftOf(await onSave(changes)));
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const harness = effective?.harness_prompt;
  const effortText = (value: string | null) => value || "runtime default";
  const profileText = (value: EffectiveProfile) => `${value.name} (${value.runtime} · ${value.provider} · ${value.model})`;
  const budgetText = (value: number | null) => (value == null ? "no cap" : formatUsd(value));
  const concurrencyText = (value: number) => String(value);

  const profileField = (
    <div className="space-y-1 sm:col-span-2">
      <label htmlFor={`${idPrefix}-profile`} className="text-xs font-medium text-muted-foreground">
        {AGENT_PROFILE.name}
      </label>
      <AgentProfileSelect
        id={`${idPrefix}-profile`}
        value={draft.profile}
        onChange={(profile) => set({ profile })}
        emptyLabel={isSystem ? `Built-in: ${limits.builtin_profile.name}` : "Inherit"}
        roles={roles}
        describedBy={`${idPrefix}-profile-now`}
        unlistedName={effective?.profile.value.profile_id === draft.profile ? effective.profile.value.name : undefined}
      />
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <NowValue id={`${idPrefix}-profile-now`} setting={effective?.profile} layer={layer} show={profileText} />
        {!isSystem && draft.profile && (
          <button
            type="button"
            onClick={() => set({ profile: "" })}
            className="text-[11px] text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            Remove override
          </button>
        )}
      </div>
    </div>
  );

  return (
    <form onSubmit={save} className="space-y-4">
      {profileFirst && profileField}
      {profileFirst && (
        <h4 className="border-t border-border pt-4 text-xs font-semibold">More overrides</h4>
      )}
      <div className="space-y-1">
        <HarnessField
          id={`${idPrefix}-harness`}
          label="Harness prompt"
          value={draft.harness}
          onChange={(harness) => set({ harness })}
          inherits={HARNESS_INHERITS[layer]}
          rows={draft.harness.trim() ? 10 : 3}
          hint={harnessHint}
        />
        {harness && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Now>
              Now: {harness.value.length} chars · {sourceText(harness.source, layer, harness.from_role)}
            </Now>
            {!draft.harness.trim() && (
              <button
                type="button"
                onClick={() => set({ harness: harness.value })}
                className="text-[11px] text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                Start from it
              </button>
            )}
          </div>
        )}
        {!harness && isSystem && !draft.harness.trim() && (
          <button
            type="button"
            onClick={() => set({ harness: limits.builtin_harness_prompt })}
            className="text-[11px] text-primary underline-offset-2 hover:underline"
          >
            Start from the built-in text
          </button>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {!profileFirst && profileField}
        <div className="space-y-1">
          <label htmlFor={`${idPrefix}-effort`} className="text-xs font-medium text-muted-foreground">Effort</label>
          <select
            id={`${idPrefix}-effort`}
            value={draft.effort}
            onChange={(e) => set({ effort: e.target.value })}
            aria-describedby={`${idPrefix}-effort-now`}
            className={SELECT}
          >
            <option value="">{isSystem ? `The ${AGENT_PROFILE.name}'s or the runtime's default` : "Inherit"}</option>
            {limits.effort_levels.map((level) => (
              <option key={level} value={level}>{level}</option>
            ))}
          </select>
          <NowValue id={`${idPrefix}-effort-now`} setting={effective?.effort} layer={layer} show={effortText} />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${idPrefix}-budget`} className="text-xs font-medium text-muted-foreground">
            Spend cap per run (USD)
          </label>
          <Input
            id={`${idPrefix}-budget`}
            inputMode="decimal"
            value={draft.budget}
            onChange={(e) => set({ budget: e.target.value })}
            placeholder={isSystem ? "No cap" : "Inherit"}
            aria-describedby={`${idPrefix}-budget-now`}
          />
          <NowValue id={`${idPrefix}-budget-now`} setting={effective?.run_budget_usd} layer={layer} show={budgetText} />
        </div>
        {concurrencyRole && (
          <div className="space-y-1">
            <label htmlFor={`${idPrefix}-concurrency`} className="text-xs font-medium text-muted-foreground">
              {CONCURRENCY_LABELS[concurrencyRole]}
            </label>
            <Input
              id={`${idPrefix}-concurrency`}
              inputMode="numeric"
              value={draft.concurrency}
              onChange={(e) => set({ concurrency: e.target.value })}
              placeholder="Inherit"
              aria-describedby={`${idPrefix}-concurrency-now ${idPrefix}-concurrency-hint`}
            />
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <NowValue
                id={`${idPrefix}-concurrency-now`}
                setting={effective?.max_concurrency}
                layer={layer}
                show={concurrencyText}
              />
              {draft.concurrency && (
                <button
                  type="button"
                  onClick={() => set({ concurrency: "" })}
                  className="text-[11px] text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                >
                  Clear
                </button>
              )}
            </div>
            <p id={`${idPrefix}-concurrency-hint`} className="text-[11px] text-muted-foreground">
              1–{limits.concurrency_max}. More wait their turn; lowering it stops nothing already running.
            </p>
          </div>
        )}
      </div>

      <div className="space-y-2">
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-xs font-medium text-muted-foreground">Environment variables</span>
          <span className="text-[11px] tabular-nums text-muted-foreground">
            {draft.env.length} / {limits.env_max_entries}
          </span>
        </div>
        {draft.env.map((entry, index) => (
          <div key={index} className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <Input
                aria-label={`Variable ${index + 1} name`}
                value={entry.key}
                onChange={(e) => setEntry(index, { key: e.target.value.toUpperCase() })}
                placeholder="NAME"
                aria-invalid={!!envProblems[index]}
                className="w-2/5 min-w-32 font-mono"
              />
              <Input
                aria-label={`Variable ${index + 1} value`}
                value={entry.unset ? "" : entry.value}
                onChange={(e) => setEntry(index, { value: e.target.value })}
                placeholder={entry.unset ? "unset here" : "value"}
                disabled={entry.unset}
                className="min-w-32 flex-1 font-mono"
              />
              {!isSystem && (
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-primary"
                    checked={entry.unset}
                    onChange={(e) => setEntry(index, { unset: e.target.checked })}
                  />
                  Unset
                </label>
              )}
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => set({ env: draft.env.filter((_, i) => i !== index) })}
                aria-label={`Remove ${entry.key || `variable ${index + 1}`}`}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
            {envProblems[index] && <p className="text-xs text-destructive">{envProblems[index]}</p>}
          </div>
        ))}
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => set({ env: [...draft.env, { key: "", value: "", unset: false }] })}
          disabled={draft.env.length >= limits.env_max_entries}
        >
          <Plus className="h-3.5 w-3.5" />
          Add variable
        </Button>
        {effective && (
          <Now>
            Now:{" "}
            {effective.env.length === 0
              ? "none"
              : effective.env.map((e, i) => (
                  <span key={e.key}>
                    {i > 0 && ", "}
                    <span className="font-mono text-foreground">{e.key}</span> {sourceText(e.source, layer)}
                  </span>
                ))}
          </Now>
        )}
        <p className="text-xs text-muted-foreground">
          {isSystem
            ? "Added on top of the server's environment."
            : "Added to or overriding the variables of the layers below; Unset removes one they set."}{" "}
          Not for secrets: values are shown here, and run records keep the names with a hash of each value.
        </p>
      </div>

      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {saved && !dirty && (
          <span role="status" className="mr-auto flex items-center gap-1 text-xs text-success">
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            Saved; applies to the next run
          </span>
        )}
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={!dirty || busy}
          onClick={() => {
            setDraft(draftOf(values));
            setError("");
          }}
        >
          Discard changes
        </Button>
        <Button type="submit" size="sm" disabled={!dirty || busy || invalid} className={cn(busy && "cursor-wait")}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}
