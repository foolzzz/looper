import { useCallback, useEffect, useId, useRef, useState } from "react";
import { AlertCircle, ArrowRight, CheckCircle2, Loader2, Play, Plus, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { AgentProfileSelect } from "@/components/agent-profiles";
import { CollapsibleRow } from "@/components/collapsible-row";
import { LABEL } from "@/components/registry-ui";
import {
  createSchedule,
  deleteSchedule,
  fetchSchedule,
  fetchScheduleRuns,
  fetchSchedules,
  previewSchedule,
  runSchedule,
  updateSchedule,
  type Project,
  type ScheduleCatchUp,
  type ScheduleFields,
  type ScheduleInfo,
  type ScheduleLimits,
  type ScheduleOverlap,
  type SchedulePreview,
  type ScheduleRunInfo,
  type ScheduleRunStatus,
  type ScheduleUpdate,
  type TaskRole,
} from "@/lib/api";
import { roleLabel } from "@/lib/runtime-catalog";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { errorText } from "@/lib/use-action";
import { useCollapsibleRows } from "@/lib/use-collapsible-rows";
import { formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";

/** How often the list (next run, last result) and the history of an open row with a run going refresh. */
const REFRESH_MS = 10_000;
/** How long the answer to "Run now" or a note stays beside the row's controls; errors stay. */
const NOTICE_MS = 10_000;
/** How long the rule waits for typing to pause before the server checks it. */
const PREVIEW_DELAY_MS = 300;
const PREVIEW_COUNT = 5;
const ROLES: TaskRole[] = ["planner", "worker", "reviewer"];
const TEXTAREA =
  "w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 text-sm text-foreground shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring aria-invalid:border-destructive";

type Variant = "default" | "success" | "warning" | "destructive" | "outline";

const RUN_STATUS: Record<ScheduleRunStatus, { label: string; variant: Variant }> = {
  queued: { label: "Queued", variant: "warning" },
  running: { label: "Running", variant: "default" },
  completed: { label: "Completed", variant: "success" },
  failed: { label: "Failed", variant: "destructive" },
  stopped: { label: "Stopped", variant: "warning" },
  cancelled: { label: "Cancelled", variant: "outline" },
  timed_out: { label: "Timed out", variant: "destructive" },
  skipped_overlap: { label: "Skipped: busy", variant: "outline" },
  skipped_missed: { label: "Skipped: missed", variant: "outline" },
};

const isActiveRun = (run: ScheduleRunInfo) => run.status === "running" || run.status === "queued";

function RunStatusBadge({ status }: { status: ScheduleRunStatus }) {
  const shown = RUN_STATUS[status] ?? { label: status, variant: "outline" };
  return (
    <Badge variant={shown.variant} className="shrink-0">
      {shown.label}
    </Badge>
  );
}

function triggerText(run: ScheduleRunInfo) {
  if (run.trigger === "manual") return "Manual";
  if (run.trigger === "catch_up") return `Catch-up (${run.missed} missed)`;
  return "Scheduled";
}

const formats = new Map<string, Intl.DateTimeFormat>();

/** A time as the schedule's own zone shows it: "Tue, Oct 15, 09:00 PDT". */
function formatInZone(iso: string | null, timeZone: string): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (isNaN(date.getTime())) return iso;
  let format = formats.get(timeZone);
  if (!format) {
    const options: Intl.DateTimeFormatOptions = {
      weekday: "short",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    };
    try {
      format = new Intl.DateTimeFormat(undefined, { ...options, timeZone });
    } catch {
      // A zone this browser does not know: its own zone, still named.
      format = new Intl.DateTimeFormat(undefined, options);
    }
    formats.set(timeZone, format);
  }
  return format.format(date);
}

let zoneList: string[] | null = null;

/** The browser's IANA time zones, for the zone field's suggestions; empty where it cannot list them. */
function timeZones(): string[] {
  if (!zoneList) {
    try {
      const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
      zoneList = zones.length && !zones.includes("UTC") ? ["UTC", ...zones] : zones;
    } catch {
      zoneList = [];
    }
  }
  return zoneList;
}

function omit<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

const byName = (a: ScheduleInfo, b: ScheduleInfo) => {
  const x = a.name.toLowerCase();
  const y = b.name.toLowerCase();
  return x < y ? -1 : x > y ? 1 : 0;
};

/** "5 minutes" or "90 seconds". */
function interval(seconds: number) {
  if (seconds % 60) return `${seconds} seconds`;
  const minutes = seconds / 60;
  return `${minutes} ${minutes === 1 ? "minute" : "minutes"}`;
}

// --- The form ---

/** A schedule as its form holds it: text as typed. */
interface Draft {
  name: string;
  cron: string;
  timezone: string;
  requirement: string;
  workDir: string;
  overlap: ScheduleOverlap;
  catchUp: ScheduleCatchUp;
  /** Minutes; "" = no limit. */
  timeout: string;
  /** USD; "" = the project's task cap. */
  budget: string;
  profiles: Partial<Record<TaskRole, string>>;
}

function draftOf(schedule: ScheduleInfo | null, limits: ScheduleLimits): Draft {
  if (!schedule)
    return {
      name: "",
      cron: "",
      timezone: limits.default_timezone,
      requirement: "",
      workDir: "",
      overlap: "skip",
      catchUp: "once",
      timeout: "",
      budget: "",
      profiles: {},
    };
  return {
    name: schedule.name,
    cron: schedule.cron,
    timezone: schedule.timezone,
    requirement: schedule.requirement,
    workDir: schedule.work_dir,
    overlap: schedule.overlap,
    catchUp: schedule.catch_up,
    timeout: schedule.run_timeout_seconds == null ? "" : String(schedule.run_timeout_seconds / 60),
    budget: schedule.run_budget_usd == null ? "" : String(schedule.run_budget_usd),
    profiles: { ...schedule.profile_overrides },
  };
}

/** A number field's value: null when empty, NaN when it is not a number (its range is the server's to judge). */
function numberOf(text: string): number | null {
  const value = text.trim();
  return value === "" ? null : Number(value);
}

/** The draft as the API takes it; an empty zone is the default one. */
function fieldsOf(draft: Draft, limits: ScheduleLimits): Required<ScheduleFields> {
  const minutes = numberOf(draft.timeout);
  return {
    name: draft.name.trim(),
    cron: draft.cron.trim(),
    requirement: draft.requirement.trim(),
    timezone: draft.timezone.trim() || limits.default_timezone,
    work_dir: draft.workDir.trim(),
    overlap: draft.overlap,
    catch_up: draft.catchUp,
    run_timeout_seconds: minutes == null ? null : Math.round(minutes * 60),
    run_budget_usd: numberOf(draft.budget),
    profile_overrides: Object.fromEntries(Object.entries(draft.profiles).filter(([, profile]) => profile)),
  };
}

const sameProfiles = (a: Partial<Record<TaskRole, string>>, b: Partial<Record<TaskRole, string>>) =>
  ROLES.every((role) => (a[role] || "") === (b[role] || ""));

/** What the fields change, as a PUT takes it; `profile_overrides` is sent whole. */
function changesOf(fields: Required<ScheduleFields>, schedule: ScheduleInfo): ScheduleUpdate {
  const changes: ScheduleUpdate = {};
  for (const key of ["name", "cron", "requirement", "timezone", "work_dir", "overlap", "catch_up"] as const) {
    if (fields[key] !== schedule[key]) (changes as Record<string, unknown>)[key] = fields[key];
  }
  if (fields.run_timeout_seconds !== schedule.run_timeout_seconds) changes.run_timeout_seconds = fields.run_timeout_seconds;
  if (fields.run_budget_usd !== schedule.run_budget_usd) changes.run_budget_usd = fields.run_budget_usd;
  if (!sameProfiles(fields.profile_overrides, schedule.profile_overrides))
    changes.profile_overrides = fields.profile_overrides;
  return changes;
}

/** A new schedule's fields: the optional ones that are empty are left out, so the server's defaults apply. */
function createFields(fields: Required<ScheduleFields>): ScheduleFields {
  const { work_dir, run_timeout_seconds, run_budget_usd, profile_overrides, ...rest } = fields;
  return {
    ...rest,
    ...(work_dir ? { work_dir } : {}),
    ...(run_timeout_seconds != null ? { run_timeout_seconds } : {}),
    ...(run_budget_usd != null ? { run_budget_usd } : {}),
    ...(Object.keys(profile_overrides).length ? { profile_overrides } : {}),
  };
}

/** The server's check of a rule in a zone, asked once typing pauses; only the latest request's answer counts. Null
 * while there is no rule or its check has not answered yet. */
function useRulePreview(cron: string, timezone: string) {
  const [state, setState] = useState<{ key: string; preview?: SchedulePreview; error?: string } | null>(null);
  const latest = useRef(0);
  const key = `${cron}\n${timezone}`;
  useEffect(() => {
    const mine = ++latest.current;
    if (!cron) return;
    const timer = setTimeout(() => {
      previewSchedule({ cron, timezone, count: PREVIEW_COUNT })
        .then((preview) => mine === latest.current && setState({ key, preview }))
        .catch((err) => mine === latest.current && setState({ key, error: errorText(err) }));
    }, PREVIEW_DELAY_MS);
    return () => clearTimeout(timer);
  }, [cron, timezone, key]);
  return cron && state?.key === key ? state : null;
}

function RulePreview({
  id,
  cron,
  check,
}: {
  id: string;
  cron: string;
  check: ReturnType<typeof useRulePreview>;
}) {
  const preview = check?.preview;
  return (
    <div className="space-y-1.5 rounded-md border border-border bg-muted/20 px-3 py-2">
      {!cron ? (
        <p className="text-xs text-muted-foreground">Enter a rule to see when it fires.</p>
      ) : (
        !check && (
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            Checking the rule…
          </p>
        )
      )}
      {/* Only the answer is announced, once per check. */}
      <div id={id} aria-live="polite" className="text-xs">
        {!cron || !check ? null : check.error ? (
          <p className="text-destructive">Could not check the rule: {check.error}</p>
        ) : preview && !preview.valid ? (
          <p className="flex items-start gap-1.5 text-destructive">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 break-words">{preview.error || "This rule is not valid."}</span>
          </p>
        ) : (
          preview && <p className="break-words font-medium text-foreground">{preview.description}</p>
        )}
      </div>
      {preview?.valid && preview.next.length > 0 && (
        <div className="text-xs">
          <p className="text-muted-foreground">Next {preview.next.length} fires</p>
          <ol className="mt-0.5 space-y-0.5 tabular-nums text-foreground/90">
            {preview.next.map((time) => (
              <li key={time}>
                <time dateTime={time}>{formatInZone(time, preview.timezone)}</time>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

/** One of a setting's options, with what it does. */
function Choices<T extends string>({
  name,
  legend,
  value,
  options,
  onChange,
}: {
  name: string;
  legend: string;
  value: T;
  options: { value: T; label: string; hint: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <fieldset className="space-y-1.5">
      <legend className={cn(LABEL, "mb-1.5")}>{legend}</legend>
      {options.map((option) => (
        <label
          key={option.value}
          className="flex min-h-11 cursor-pointer items-start gap-2.5 rounded-md border border-border px-3 py-2 transition-colors hover:bg-accent/50 has-checked:border-primary/60 motion-reduce:transition-none"
        >
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={value === option.value}
            onChange={() => onChange(option.value)}
            className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
          />
          <span className="min-w-0 text-xs">
            <span className="font-medium text-foreground">{option.label}</span>
            <span className="block text-muted-foreground">{option.hint}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

/**
 * Every field of a schedule, with the live check of its rule. Null `schedule`: a new one, created disabled. The
 * server's refusal (400, 409) is shown as it gives it.
 */
function ScheduleForm({
  idPrefix,
  project,
  schedule,
  limits,
  zonesId,
  onDirtyChange,
  onSaved,
  onDeleted,
  onCancel,
}: {
  idPrefix: string;
  project: Project;
  schedule: ScheduleInfo | null;
  limits: ScheduleLimits;
  /** The page's list of time zones, for the zone field. */
  zonesId: string;
  onDirtyChange?: (dirty: boolean) => void;
  onSaved: (schedule: ScheduleInfo) => void;
  onDeleted?: () => void;
  /** A new schedule's Cancel. */
  onCancel?: () => void;
}) {
  const [draft, setDraft] = useState(() => draftOf(schedule, limits));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setSaved(false);
  };
  const fields = fieldsOf(draft, limits);
  const changes = schedule ? changesOf(fields, schedule) : {};
  const dirty = Object.keys(changes).length > 0;
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);

  const check = useRulePreview(fields.cron, fields.timezone);
  const ruleInvalid = check?.preview?.valid === false;
  const missing = [
    !fields.name && "a name",
    !fields.cron && "a rule",
    !fields.requirement && "a requirement",
    !fields.work_dir && !project.work_dir && "a work directory",
  ].filter(Boolean);
  const tooLong = fields.requirement.length > limits.requirement_max_chars;
  const badTimeout = Number.isNaN(fields.run_timeout_seconds);
  const badBudget = Number.isNaN(fields.run_budget_usd);
  const invalid = missing.length > 0 || tooLong || ruleInvalid || badTimeout || badBudget;
  const id = (field: string) => `${idPrefix}-${field}`;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (invalid || (schedule && !dirty)) return;
    setBusy(true);
    setError("");
    try {
      const next = schedule
        ? await updateSchedule(schedule.schedule_id, changes)
        : await createSchedule(project.project_id, createFields(fields));
      if (schedule) {
        setDraft(draftOf(next, limits));
        setSaved(true);
      }
      onSaved(next);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!schedule) return;
    setBusy(true);
    setError("");
    try {
      await deleteSchedule(schedule.schedule_id);
      onDeleted?.();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor={id("name")} className={LABEL}>Name *</label>
          <Input
            id={id("name")}
            value={draft.name}
            maxLength={limits.name_max_chars}
            onChange={(e) => set("name", e.target.value)}
            placeholder="Nightly dependency check"
            autoFocus={!schedule}
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={id("dir")} className={LABEL}>Work directory{project.work_dir ? "" : " *"}</label>
          <Input
            id={id("dir")}
            value={draft.workDir}
            onChange={(e) => set("workDir", e.target.value)}
            placeholder={project.work_dir ? `Default: ${project.work_dir}` : "Required: the project has no default directory"}
            aria-describedby={id("dir-hint")}
            spellCheck={false}
            className="font-mono"
          />
          <p id={id("dir-hint")} className="text-xs text-muted-foreground">
            {project.work_dir && "Empty: the project's default directory when it fires. "}
            An absolute path or one starting with ~/.
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={id("cron")} className={LABEL}>Rule (cron) *</label>
          <Input
            id={id("cron")}
            value={draft.cron}
            onChange={(e) => set("cron", e.target.value)}
            placeholder="0 9 * * 1-5"
            spellCheck={false}
            autoComplete="off"
            aria-invalid={ruleInvalid}
            aria-describedby={`${id("cron-hint")} ${id("preview")}`}
            className="font-mono aria-invalid:border-destructive"
          />
          <p id={id("cron-hint")} className="text-xs text-muted-foreground">
            Minute, hour, day of month, month, day of week. Fires at least {interval(limits.min_interval_seconds)} apart.
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={id("zone")} className={LABEL}>Time zone</label>
          <Input
            id={id("zone")}
            value={draft.timezone}
            onChange={(e) => set("timezone", e.target.value)}
            placeholder={limits.default_timezone}
            list={zonesId}
            spellCheck={false}
            autoComplete="off"
            aria-describedby={id("zone-hint")}
          />
          <p id={id("zone-hint")} className="text-xs text-muted-foreground">
            An IANA name such as Europe/Berlin; the rule is read in it. Empty: {limits.default_timezone}.
          </p>
        </div>
        <div className="sm:col-span-2">
          <RulePreview id={id("preview")} cron={fields.cron} check={check} />
        </div>
      </div>

      <div className="space-y-1">
        <div className="flex items-baseline justify-between gap-2">
          <label htmlFor={id("requirement")} className={LABEL}>Requirement *</label>
          <span
            id={id("requirement-count")}
            className={cn(
              "flex items-center gap-1 text-[11px] tabular-nums",
              tooLong ? "text-destructive" : "text-muted-foreground",
            )}
          >
            {tooLong && <AlertCircle className="h-3 w-3" aria-label="Too long" />}
            {fields.requirement.length} / {limits.requirement_max_chars}
          </span>
        </div>
        <textarea
          id={id("requirement")}
          value={draft.requirement}
          onChange={(e) => set("requirement", e.target.value)}
          rows={4}
          aria-invalid={tooLong}
          aria-describedby={`${id("requirement-count")} ${id("requirement-hint")}`}
          placeholder="What each run's task should do"
          className={TEXTAREA}
        />
        <p id={id("requirement-hint")} className="text-xs text-muted-foreground">
          Each firing starts a new task of this project with this requirement.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Choices
          name={id("overlap")}
          legend="While the previous run is in progress"
          value={draft.overlap}
          onChange={(value) => set("overlap", value)}
          options={[
            { value: "skip", label: "Skip", hint: "Skip this firing while the previous run is in progress." },
            { value: "queue", label: "Queue", hint: "Start it when the previous run ends; at most one waits." },
          ]}
        />
        <Choices
          name={id("catch-up")}
          legend="Missed fires"
          value={draft.catchUp}
          onChange={(value) => set("catchUp", value)}
          options={[
            {
              value: "once",
              label: "Run once",
              hint: "After the server was down or the computer slept, run once for all missed fires.",
            },
            { value: "skip", label: "Skip", hint: "Skip them, unless the latest was within 2 minutes." },
          ]}
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor={id("timeout")} className={LABEL}>Run timeout (minutes)</label>
          <Input
            id={id("timeout")}
            inputMode="decimal"
            value={draft.timeout}
            onChange={(e) => set("timeout", e.target.value)}
            placeholder="No limit"
            aria-invalid={badTimeout}
            aria-describedby={id("timeout-hint")}
            className="aria-invalid:border-destructive"
          />
          <p id={id("timeout-hint")} className={cn("text-xs", badTimeout ? "text-destructive" : "text-muted-foreground")}>
            {badTimeout
              ? "Enter the minutes as a number, or leave it empty."
              : "Empty: no limit. A run still going then is stopped and counts as a failure."}
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={id("budget")} className={LABEL}>Spend cap per run (USD)</label>
          <Input
            id={id("budget")}
            inputMode="decimal"
            value={draft.budget}
            onChange={(e) => set("budget", e.target.value)}
            placeholder={project.task_budget_usd != null ? `Project cap: ${formatUsd(project.task_budget_usd)}` : "No cap"}
            aria-invalid={badBudget}
            aria-describedby={id("budget-hint")}
            className="aria-invalid:border-destructive"
          />
          <p id={id("budget-hint")} className={cn("text-xs", badBudget ? "text-destructive" : "text-muted-foreground")}>
            {badBudget
              ? "Enter dollars as a number, or leave it empty."
              : "Empty: the project's spend cap per task applies (its Settings tab)."}
          </p>
        </div>
      </div>

      <fieldset className="space-y-2">
        <legend className={cn(LABEL, "mb-2")}>{AGENT_PROFILE.plural}</legend>
        <div className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-2">
          {(limits.roles?.length ? limits.roles : ROLES).map((role) => (
            <div key={role} className="contents">
              <label htmlFor={id(`profile-${role}`)} className="pt-2 text-xs text-muted-foreground">
                {roleLabel(role)}
              </label>
              <AgentProfileSelect
                id={id(`profile-${role}`)}
                value={draft.profiles[role] ?? ""}
                onChange={(profile) => set("profiles", { ...draft.profiles, [role]: profile })}
                emptyLabel="From the project's settings"
                roles={[role]}
              />
            </div>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          Optional. A chosen {AGENT_PROFILE.name} runs that role in this schedule&apos;s tasks, over the project&apos;s{" "}
          {AGENT_PROFILE.plural} tab and Settings → Roles.
        </p>
      </fieldset>

      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {schedule &&
          (confirmDelete ? (
            <span className="mr-auto flex flex-wrap items-center gap-2">
              <span className="text-xs text-muted-foreground">Delete {schedule.name}? Its tasks stay.</span>
              <Button type="button" variant="destructive" size="sm" onClick={remove} disabled={busy}>
                {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                Delete
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmDelete(false)} disabled={busy}>
                Cancel
              </Button>
            </span>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setConfirmDelete(true)}
              className="mr-auto text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete schedule
            </Button>
          ))}
        {saved && !dirty && (
          <span role="status" className="flex items-center gap-1 text-xs text-success">
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            Saved
          </span>
        )}
        {missing.length > 0 && (dirty || !schedule) && (
          <span className="text-xs text-muted-foreground">Needs {missing.join(", ")}</span>
        )}
        {schedule ? (
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!dirty || busy}
            onClick={() => {
              setDraft(draftOf(schedule, limits));
              setError("");
            }}
          >
            Discard changes
          </Button>
        ) : (
          <Button type="button" variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
        )}
        <Button type="submit" size="sm" disabled={(schedule ? !dirty : false) || busy || invalid}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {schedule ? "Save" : "Create"}
        </Button>
      </div>
    </form>
  );
}

// --- Run history ---

/** A schedule's runs, newest first: loaded when its row first opens, after "Run now" (`version`) and when the
 * newest run changes, and every 10 s while the row is open with a run going. */
function RunHistory({ schedule, open, version }: { schedule: ScheduleInfo; open: boolean; version: number }) {
  const [runs, setRuns] = useState<ScheduleRunInfo[] | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);
  const scheduleId = schedule.schedule_id;
  const newest = schedule.last_run ? `${schedule.last_run.run_id}:${schedule.last_run.status}` : "";

  const load = useCallback(() => {
    const mine = ++latest.current;
    fetchScheduleRuns(scheduleId)
      .then((next) => {
        if (mine !== latest.current) return;
        setRuns(next);
        setError("");
      })
      .catch((err) => mine === latest.current && setError(errorText(err)));
  }, [scheduleId]);

  useEffect(() => load(), [load, version, newest]);
  const active = !!runs?.some(isActiveRun);
  useEffect(() => {
    if (!open || !active) return;
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [open, active, load]);

  return (
    <section aria-labelledby={`${scheduleId}-history`} className="space-y-2 border-t border-border pt-4">
      <h3 id={`${scheduleId}-history`} className="text-xs font-semibold">Run history</h3>
      {error && <p role="alert" className="text-xs text-destructive">Could not load the runs: {error}</p>}
      {!runs ? (
        !error && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />
      ) : runs.length === 0 ? (
        <p className="text-xs text-muted-foreground">No runs yet</p>
      ) : (
        <ul className="divide-y divide-border">
          {runs.map((run) => {
            const when = run.fired_at ?? run.scheduled_for;
            const text = run.error ?? run.summary;
            return (
              <li key={run.run_id} className="flex flex-col gap-1 py-2 sm:flex-row sm:items-center sm:gap-3">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs sm:shrink-0">
                  <time dateTime={when} className="tabular-nums text-foreground">
                    {formatInZone(when, schedule.timezone)}
                  </time>
                  <span className="text-muted-foreground">{triggerText(run)}</span>
                  <RunStatusBadge status={run.status} />
                  <span className="font-mono text-muted-foreground" title="What its task spent">
                    {run.cost_usd == null ? "—" : formatUsd(run.cost_usd)}
                  </span>
                </div>
                {text && (
                  <p className="min-w-0 truncate text-xs text-muted-foreground sm:flex-1" title={text}>
                    {text}
                  </p>
                )}
                {run.task_id && (
                  <a
                    href={`#tasks/${encodeURIComponent(run.task_id)}`}
                    className="inline-flex shrink-0 items-center gap-1 self-start rounded-sm text-xs font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring sm:ml-auto sm:self-auto"
                  >
                    Its task
                    <ArrowRight className="h-3 w-3" aria-hidden="true" />
                  </a>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// --- The tab ---

interface Notice {
  text: string;
  tone: "ok" | "warn" | "error";
}

/** What "Run now" answered, in words. */
function runNotice(run: ScheduleRunInfo): Notice {
  switch (run.status) {
    case "running":
      return { text: "Started", tone: "ok" };
    case "queued":
      return { text: "Queued behind the running task", tone: "ok" };
    case "skipped_overlap":
      return { text: "Skipped: the previous run is still in progress", tone: "warn" };
    default:
      return {
        text: run.error ? `Not started: ${run.error}` : RUN_STATUS[run.status]?.label ?? run.status,
        tone: run.status === "failed" || run.status === "timed_out" ? "error" : "warn",
      };
  }
}

/** A message per schedule beside its controls; all but errors go after a while. */
function useNotices() {
  const [notices, setNotices] = useState<Record<string, Notice>>({});
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  useEffect(() => {
    const pending = timers.current;
    return () => Object.values(pending).forEach(clearTimeout);
  }, []);
  const notify = useCallback((scheduleId: string, notice: Notice | null) => {
    clearTimeout(timers.current[scheduleId]);
    setNotices((current) => (notice ? { ...current, [scheduleId]: notice } : omit(current, scheduleId)));
    if (notice && notice.tone !== "error")
      timers.current[scheduleId] = setTimeout(
        () => setNotices((current) => (current[scheduleId] === notice ? omit(current, scheduleId) : current)),
        NOTICE_MS,
      );
  }, []);
  return { notices, notify };
}

function NoticeText({ notice }: { notice: Notice }) {
  const Icon = notice.tone === "ok" ? CheckCircle2 : AlertCircle;
  return (
    <p
      role={notice.tone === "error" ? "alert" : "status"}
      className={cn(
        "flex basis-full items-start gap-1 text-xs sm:justify-end",
        notice.tone === "ok" ? "text-success" : notice.tone === "warn" ? "text-warning" : "text-destructive",
      )}
    >
      <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 break-words">{notice.text}</span>
    </p>
  );
}

/** The last run's result and the failures in a row, as a schedule's row shows them. */
function LastRun({ schedule }: { schedule: ScheduleInfo }) {
  const failures = schedule.consecutive_failures;
  return (
    <>
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {schedule.last_run ? (
          <>
            Last run
            <RunStatusBadge status={schedule.last_run.status} />
          </>
        ) : (
          "No runs yet"
        )}
      </span>
      {failures > 0 && (
        <Badge variant={failures >= 3 ? "destructive" : "warning"} className="shrink-0 gap-1">
          <AlertCircle className="h-3 w-3" aria-hidden="true" />
          {failures === 1 ? "1 failure" : `${failures} failures in a row`}
        </Badge>
      )}
    </>
  );
}

/**
 * A project's Schedules tab: one row per schedule (its rule, next run, last result, an on/off switch and "Run now"),
 * opening to every field and its run history, and a form for a new one. `linked`
 * (#projects/<id>/schedules/<schedule id>) opens one row and scrolls it into view; `onLink` changes that address.
 */
export function ProjectSchedulesSection({
  project,
  linked,
  onLink,
}: {
  project: Project;
  linked: string;
  onLink: (scheduleId: string) => void;
}) {
  const id = useId();
  const projectId = project.project_id;
  const [data, setData] = useState<{ schedules: ScheduleInfo[]; limits: ScheduleLimits } | null>(null);
  const [error, setError] = useState("");
  const [adding, setAdding] = useState(false);
  /** The enabled value being saved, by schedule. */
  const [switching, setSwitching] = useState<Record<string, boolean>>({});
  /** Schedules whose "Run now" is on its way. */
  const [starting, setStarting] = useState<Record<string, boolean>>({});
  /** Bumped to reload a schedule's run history. */
  const [runsVersion, setRunsVersion] = useState<Record<string, number>>({});
  const { notices, notify } = useNotices();
  const row = useCollapsibleRows(linked, data !== null);
  const latest = useRef(0);

  const load = useCallback(() => {
    // A slow poll must not overwrite a newer answer, nor one this page just wrote.
    const mine = ++latest.current;
    fetchSchedules(projectId)
      .then((next) => {
        if (mine !== latest.current) return;
        setData(next);
        setError("");
      })
      .catch((err) => mine === latest.current && setError(errorText(err)));
  }, [projectId]);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  /** Put the server's answer for one schedule into the list. */
  const put = (next: ScheduleInfo) => {
    latest.current++;
    setData((d) =>
      d && { ...d, schedules: [...d.schedules.filter((s) => s.schedule_id !== next.schedule_id), next].sort(byName) },
    );
  };

  const toggle = async (schedule: ScheduleInfo, enabled: boolean) => {
    const sid = schedule.schedule_id;
    setSwitching((s) => ({ ...s, [sid]: enabled }));
    notify(sid, null);
    try {
      put(await updateSchedule(sid, { enabled }));
    } catch (err) {
      notify(sid, { text: `Could not turn it ${enabled ? "on" : "off"}: ${errorText(err)}`, tone: "error" });
    } finally {
      setSwitching((s) => omit(s, sid));
    }
  };

  const runNow = async (schedule: ScheduleInfo) => {
    const sid = schedule.schedule_id;
    setStarting((s) => ({ ...s, [sid]: true }));
    notify(sid, null);
    try {
      notify(sid, runNotice(await runSchedule(sid)));
      setRunsVersion((v) => ({ ...v, [sid]: (v[sid] ?? 0) + 1 }));
      fetchSchedule(sid).then(put, () => {});
    } catch (err) {
      notify(sid, { text: errorText(err), tone: "error" });
    } finally {
      setStarting((s) => omit(s, sid));
    }
  };

  const zones = timeZones();
  const zonesId = `${id}-zones`;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <p className="min-w-0 flex-1 basis-64 text-sm text-muted-foreground">
          A schedule runs a full task of this project on a time rule. A new one starts disabled; turn it on when it is
          ready.
        </p>
        <Button type="button" size="sm" onClick={() => setAdding(true)} disabled={!data || adding}>
          <Plus className="h-3.5 w-3.5" />
          New schedule
        </Button>
      </div>

      {zones.length > 0 && (
        <datalist id={zonesId}>
          {zones.map((zone) => (
            <option key={zone} value={zone} />
          ))}
        </datalist>
      )}

      {!data ? (
        error ? (
          <div className="flex flex-wrap items-center gap-3">
            <p role="alert" className="text-xs text-destructive">Could not load the schedules: {error}</p>
            <Button type="button" variant="outline" size="sm" onClick={load}>
              Retry
            </Button>
          </div>
        ) : (
          <div className="flex h-24 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-label="Loading" />
          </div>
        )
      ) : (
        <div className="space-y-2">
          {error && <p role="alert" className="text-xs text-destructive">Could not refresh the schedules: {error}</p>}

          {adding && (
            <section aria-labelledby={`${id}-new`} className="rounded-lg border border-border bg-card">
              <h2 id={`${id}-new`} className="border-b border-border px-5 py-3 text-sm font-semibold">
                New schedule
              </h2>
              <div className="px-5 py-4">
                <ScheduleForm
                  idPrefix={`${id}-new`}
                  project={project}
                  schedule={null}
                  limits={data.limits}
                  zonesId={zonesId}
                  onCancel={() => setAdding(false)}
                  onSaved={(created) => {
                    put(created);
                    setAdding(false);
                    notify(created.schedule_id, { text: "Created disabled; turn it on to start", tone: "ok" });
                    onLink(created.schedule_id);
                  }}
                />
              </div>
            </section>
          )}

          {data.schedules.length === 0 && !adding && (
            <p className="rounded-lg border border-dashed border-border px-5 py-6 text-center text-sm text-muted-foreground">
              No schedules yet
            </p>
          )}

          {data.schedules.map((schedule) => {
            const sid = schedule.schedule_id;
            const state = row(sid);
            const enabled = switching[sid] ?? schedule.enabled;
            const next = schedule.enabled
              ? schedule.next_fire_at
                ? `next ${formatInZone(schedule.next_fire_at, schedule.timezone)}`
                : "no next run"
              : "Disabled";
            return (
              <CollapsibleRow
                key={sid}
                id={`schedule-${sid}`}
                {...state}
                name={schedule.name}
                detail={schedule.description}
                summary={next}
                actions={
                  <>
                    {/* On a phone the summary is cut short: the next run again, for the eye only (the row's button
                        names it). */}
                    <span aria-hidden="true" className="basis-full truncate text-xs text-muted-foreground sm:hidden">
                      {next.charAt(0).toUpperCase() + next.slice(1)} · {schedule.description}
                    </span>
                    <LastRun schedule={schedule} />
                    <span className="ml-auto flex items-center gap-2">
                      <label className="flex h-11 cursor-pointer items-center gap-2 text-xs text-muted-foreground sm:h-9">
                        <Switch
                          checked={enabled}
                          onChange={(on) => toggle(schedule, on)}
                          disabled={sid in switching}
                          label={`Enable ${schedule.name}`}
                        />
                        <span className="w-6" aria-hidden="true">{enabled ? "On" : "Off"}</span>
                      </label>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => runNow(schedule)}
                        disabled={!!starting[sid]}
                        aria-label={`Run now: ${schedule.name}`}
                        className="h-11 sm:h-8"
                      >
                        {starting[sid] ? (
                          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
                        ) : (
                          <Play className="h-3.5 w-3.5" aria-hidden="true" />
                        )}
                        Run now
                      </Button>
                    </span>
                    {notices[sid] && <NoticeText notice={notices[sid]} />}
                  </>
                }
              >
                {(dirty) => (
                  <>
                    <ScheduleForm
                      idPrefix={`${id}-${sid}`}
                      project={project}
                      schedule={schedule}
                      limits={data.limits}
                      zonesId={zonesId}
                      onDirtyChange={dirty.layer}
                      onSaved={put}
                      onDeleted={() => {
                        latest.current++;
                        setData((d) => d && { ...d, schedules: d.schedules.filter((s) => s.schedule_id !== sid) });
                        if (linked === sid) onLink("");
                      }}
                    />
                    <RunHistory schedule={schedule} open={state.state === "open"} version={runsVersion[sid] ?? 0} />
                  </>
                )}
              </CollapsibleRow>
            );
          })}
        </div>
      )}
    </div>
  );
}
