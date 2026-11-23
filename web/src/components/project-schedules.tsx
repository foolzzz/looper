import { useCallback, useEffect, useId, useRef, useState } from "react";
import { AlertCircle, ArrowRight, CheckCircle2, ChevronDown, ChevronRight, Loader2, Play, Plus, Trash2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { AgentProfileSelect } from "@/components/agent-profiles";
import { CollapsibleRow } from "@/components/collapsible-row";
import { LABEL, SELECT } from "@/components/registry-ui";
import {
  confirmSubscriptionTarget,
  createSchedule,
  deleteSchedule,
  fetchSchedule,
  fetchManagers,
  fetchScheduleRuns,
  fetchRunLogTail,
  downloadRunLog,
  fetchSchedules,
  previewSchedule,
  resetSchedulePlannerSession,
  resetSubscriptionCursor,
  runSchedule,
  updateSchedule,
  type Project,
  type ManagerInfo,
  type ScheduleCreate,
  type ScheduleCatchUp,
  type ScheduleFields,
  type ScheduleInfo,
  type ScheduleKind,
  type ScheduleLimits,
  type ScheduleNotify,
  type ScheduleNotifyFormat,
  type ScheduleUserGates,
  type ScheduleNotifyTarget,
  type ScheduleOverlap,
  type SchedulePreview,
  type ScheduleRunInfo,
  type ScheduleRunsPage,
  type ScheduleRunStatus,
  type RunStats,
  type ScheduleUpdate,
  type SubscriptionChatKind,
  type SubscriptionFields,
  type SubscriptionInfo,
  type SubscriptionRunInfo,
  type SubscriptionSenderKind,
  type TaskLimitRole,
  type TaskRole,
} from "@/lib/api";
import { subscriptionPreset, type SubscriptionPresetFields } from "@/lib/api-leads";
import { callV1 } from "@/lib/v1";
import { roleLabel } from "@/lib/runtime-catalog";
import {
  scheduleNotifyOptions,
  RUN_STATUS,
  TASK_LIMIT_ROLES,
  badTaskLimit,
  changedTaskLimits,
  createTaskLimits,
  omit,
  runNotice,
  taskLimitsDraft,
  taskLimitsOf,
  useNotices,
  type Notice,
  type TaskLimitsDraft,
} from "@/lib/schedules";
import { AGENT_PROFILE, settingsHref } from "@/lib/settings-pages";
import { errorText } from "@/lib/use-action";
import { useCollapsibleRows } from "@/lib/use-collapsible-rows";
import { formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";

/** How often the list (next run, last result) and the history of an open row with a run going refresh. */
const REFRESH_MS = 10_000;
/** How long the rule waits for typing to pause before the server checks it. */
const PREVIEW_DELAY_MS = 300;
const PREVIEW_COUNT = 5;
const ROLES: TaskRole[] = ["planner", "worker", "reviewer"];
const TASK_LIMIT_LABELS: Record<TaskLimitRole, string> = {
  worker: "Workers at once in each run's task",
  reviewer: "Reviewers at once in each run's task",
};
const TEXTAREA =
  "w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 text-sm text-foreground shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring aria-invalid:border-destructive";

const isActiveRun = (run: ScheduleRunInfo) =>
  run.status === "running" || run.status === "queued" || run.status === "gating";

/** A run's status as a badge; the dashboard shows a failing schedule's last one too. */
export function RunStatusBadge({ status }: { status: ScheduleRunStatus }) {
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

/** A time as the schedule's own zone shows it: "Tue, Oct 15, 09:00 PDT"; without a zone, as the browser's does. */
function formatInZone(iso: string | null, timeZone?: string): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (isNaN(date.getTime())) return iso;
  let format = formats.get(timeZone ?? "");
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
    formats.set(timeZone ?? "", format);
  }
  return format.format(date);
}

/** A usage-window wait in the schedule's own zone. */
export function usageWindowWait(until: string, timeZone?: string): string {
  return `Waiting for a usage window until ${formatInZone(until, timeZone)}`;
}

/** The schedules list's next-run line. Null when the caller shows "Next" plus the schedule's own time.
 * A disabled schedule stays labeled Disabled when it is also waiting. */
export function listNextRun(schedule: {
  enabled: boolean;
  timezone?: string;
  next_fire_at?: string | null;
  usage_hold?: { until: string } | null;
}): string | null {
  const hold = schedule.usage_hold ? usageWindowWait(schedule.usage_hold.until, schedule.timezone) : null;
  if (!schedule.enabled) return hold ? `Disabled · ${hold}` : "Disabled";
  if (hold) return hold;
  if (!schedule.next_fire_at) return "No next run";
  return null;
}

/** A `<time>` in a schedule's zone (see `formatInZone`). */
export function ScheduleTime({ iso, timeZone, className }: { iso: string; timeZone?: string; className?: string }) {
  return (
    <time dateTime={iso} className={className}>
      {formatInZone(iso, timeZone)}
    </time>
  );
}

/** The stored session stays visible for parallel schedules too, until explicitly reset. */
export function PlannerSessionStatus({ schedule }: { schedule: ScheduleInfo }) {
  const session = schedule.planner_session;
  return (
    <p className="basis-full text-xs text-muted-foreground">
      {session.active ? (
        <>
          Planner session: continuing since {formatInZone(session.updated_at, schedule.timezone)}
          {session.last_task_id && (
            <>
              {", last used by run "}
              <a
                href={`#tasks/${encodeURIComponent(session.last_task_id)}`}
                className="rounded-sm hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {session.last_task_title || session.last_task_id}
              </a>
            </>
          )}
        </>
      ) : "Planner session: none yet"}
    </p>
  );
}

export function ResetPlannerSession({ schedule, onReset }: {
  schedule: ScheduleInfo;
  onReset: (schedule: ScheduleInfo) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const reset = async () => {
    if (!confirm(`Reset the Planner session for ${schedule.name}? The next run starts a new session. A run in progress keeps its own session until it ends and will not write it back.`)) return;
    setBusy(true);
    setError("");
    try {
      await resetSchedulePlannerSession(schedule.schedule_id);
      try {
        onReset(await fetchSchedule(schedule.schedule_id));
      } catch (err) {
        setError(`Planner session reset; could not refresh the schedule: ${errorText(err)}`);
      }
    } catch (err) {
      setError(`Could not reset the Planner session: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={reset}
        disabled={busy}
        aria-label={`Reset Planner session: ${schedule.name}`}
        className="h-11 sm:h-8"
      >
        {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
        Reset Planner session
      </Button>
      {error && <p role="alert" className="basis-full text-xs text-destructive">{error}</p>}
    </>
  );
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
  kind: ScheduleKind;
  command: string;
  envText: string;
  envFile: string;
  deadline: string;
  failuresAfter: string;
  staleAfterMinutes: string;
  overlapAfter: string;
  managerId: string;
  templateKey: string;
  templateInputs: Record<string, string>;
  name: string;
  cron: string;
  timezone: string;
  requirement: string;
  workDir: string;
  overlap: ScheduleOverlap;
  /** With overlap "parallel". */
  maxParallel: string;
  catchUp: ScheduleCatchUp;
  /** Whether runs follow the system default, have the limit below, or have none. */
  timeoutMode: "default" | "limit" | "none";
  /** Minutes, with timeoutMode "limit". */
  timeout: string;
  /** USD; "" = the project's task cap. */
  budget: string;
  profiles: Partial<Record<TaskRole, string>>;
  taskLimits: TaskLimitsDraft;
  /** "" = no gate. */
  gateCommand: string;
  /** Seconds; "" = the default. */
  gateTimeout: string;
  /** USD; "" = no daily cap. */
  dailyBudget: string;
  notify: ScheduleNotify;
  notifyFormat: ScheduleNotifyFormat;
  userGates: ScheduleUserGates;
  /** `targetKey` of the target; "" = dashboard only. */
  notifyTarget: string;
}

/** A notify target as the target select holds it. */
const targetKey = (target: ScheduleNotifyTarget | null) => (target ? `${target.channel}\n${target.chat}` : "");

function targetOf(key: string): ScheduleNotifyTarget | null {
  if (!key) return null;
  const at = key.indexOf("\n");
  return { channel: key.slice(0, at), chat: key.slice(at + 1) };
}

/** A direct chat the operator can be pushed to: a Lark open id, or a positive Telegram user id. Bound groups are not. */
function isDirectChat(chat: string): boolean {
  return /^(?:ou_[0-9A-Za-z]+|[1-9][0-9]{0,19})$/.test(chat);
}

type OutputKind = "" | "push" | "leads" | "intents" | "triage";

function outputKindOf(value: string | undefined): OutputKind {
  return value === "push" || value === "leads" || value === "intents" || value === "triage" ? value : "";
}

function draftOf(schedule: ScheduleInfo | null, limits: ScheduleLimits): Draft {
  if (!schedule)
    return {
      kind: "agent", command: "", envText: "", envFile: "", deadline: "",
      failuresAfter: "", staleAfterMinutes: "", overlapAfter: "",
      managerId: "manager",
      templateKey: "",
      templateInputs: {},
      name: "",
      cron: "",
      timezone: limits.default_timezone,
      requirement: "",
      workDir: "",
      overlap: "skip",
      maxParallel: "3",
      catchUp: "once",
      timeoutMode: "default",
      timeout: "",
      budget: "",
      profiles: {},
      taskLimits: { worker: "", reviewer: "" },
      gateCommand: "",
      gateTimeout: "",
      dailyBudget: "",
      notify: "failures",
      notifyFormat: "report",
      userGates: "off",
      notifyTarget: "",
    };
  return {
    kind: schedule.kind ?? "agent",
    command: schedule.command ?? "",
    envText: environmentText(schedule.env ?? {}),
    envFile: schedule.env_file ?? "",
    deadline: schedule.success_deadline ?? "",
    failuresAfter: thresholdText(schedule, "failures_after"),
    staleAfterMinutes: thresholdText(schedule, "stale_after_seconds", 60),
    overlapAfter: thresholdText(schedule, "overlap_after"),
    managerId: schedule.manager_id,
    templateKey: schedule.template_key ?? "",
    templateInputs: { ...schedule.template_inputs },
    name: schedule.name,
    cron: schedule.cron,
    timezone: schedule.timezone,
    requirement: schedule.requirement,
    workDir: schedule.work_dir,
    overlap: schedule.overlap,
    maxParallel: String(schedule.max_parallel_runs),
    catchUp: schedule.catch_up,
    timeoutMode:
      schedule.run_timeout_seconds === "default" ? "default" : schedule.run_timeout_seconds == null ? "none" : "limit",
    timeout: typeof schedule.run_timeout_seconds === "number" ? String(schedule.run_timeout_seconds / 60) : "",
    budget: schedule.run_budget_usd == null ? "" : String(schedule.run_budget_usd),
    profiles: { ...schedule.profile_overrides },
    taskLimits: taskLimitsDraft(schedule.task_limits),
    gateCommand: schedule.gate_command ?? "",
    gateTimeout: schedule.gate_timeout_seconds == null ? "" : String(schedule.gate_timeout_seconds),
    dailyBudget: schedule.daily_budget_usd == null ? "" : String(schedule.daily_budget_usd),
    notify: schedule.notify,
    notifyFormat: schedule.notify_format ?? "report",
    userGates: schedule.user_gates,
    notifyTarget: targetKey(schedule.notify_target),
  };
}

/** A number field's value: null when empty, NaN when it is not a number (its range is the server's to judge). */
function numberOf(text: string): number | null {
  const value = text.trim();
  return value === "" ? null : Number(value);
}

function thresholdValue(schedule: ScheduleInfo, key: keyof ScheduleInfo["alert_policy"]): number | null {
  const policy = schedule.alert_policy?.[key];
  return !policy || policy.default ? null : policy.value;
}

function thresholdText(schedule: ScheduleInfo, key: keyof ScheduleInfo["alert_policy"], divisor = 1): string {
  const value = thresholdValue(schedule, key);
  return value == null ? "" : String(value / divisor);
}

function environmentText(env: Record<string, string>): string {
  const entries = Object.entries(env).sort(([a], [b]) => a.localeCompare(b));
  // JSON escapes line breaks so a value cannot become another KEY=VALUE line.
  return entries.some(([, value]) => /[\r\n]/.test(value))
    ? JSON.stringify(Object.fromEntries(entries), null, 2)
    : entries.map(([key, value]) => `${key}=${value}`).join("\n");
}

function environmentOf(text: string) {
  if (text.trimStart().startsWith("{")) {
    const error = "Environment JSON must be an object with valid environment keys and string values.";
    try {
      const env: unknown = JSON.parse(text);
      if (env && typeof env === "object" && !Array.isArray(env) && Object.entries(env).every(
        ([key, value]) => /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) && typeof value === "string",
      )) return { env: env as Record<string, string>, error: "", duplicates: [] };
    } catch { /* Keep invalid JSON visible and block submission. */ }
    return { env: {}, error, duplicates: [] };
  }
  const entries: [string, string][] = [];
  const keys = new Set<string>();
  const duplicates = new Set<string>();
  let error = "";
  text.split("\n").forEach((line, index) => {
    if (!line.trim() || line.trimStart().startsWith("#")) return;
    const at = line.indexOf("=");
    const key = line.slice(0, at).trim();
    if (at < 1 || !/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) {
      error ||= `Line ${index + 1} is not KEY=VALUE`;
      return;
    }
    if (keys.has(key)) duplicates.add(key);
    keys.add(key);
    entries.push([key, line.slice(at + 1)]);
  });
  return { env: Object.fromEntries(entries), error, duplicates: [...duplicates] };
}

type FormFields = ScheduleFields & Required<Pick<ScheduleFields,
  "name" | "cron" | "timezone" | "work_dir" | "overlap" | "max_parallel_runs" | "catch_up" |
  "run_timeout_seconds" | "notify" | "notify_format" | "notify_target" | "success_deadline" |
  "alert_failures_after" | "alert_stale_after_seconds" | "alert_overlap_after">>;

/** Command fields are kept separate from task settings before serialization. */
function fieldsOf(draft: Draft, limits: ScheduleLimits, schedule: ScheduleInfo | null): FormFields {
  const minutes = numberOf(draft.timeout);
  const common = {
    name: draft.name.trim(), cron: draft.cron.trim(),
    timezone: draft.timezone.trim() || limits.default_timezone,
    work_dir: draft.workDir.trim(), overlap: draft.overlap,
    max_parallel_runs: Number(draft.maxParallel.trim() || NaN), catch_up: draft.catchUp,
    run_timeout_seconds: draft.timeoutMode === "limit" ? Math.round((minutes ?? NaN) * 60)
      : draft.timeoutMode === "none" || draft.kind === "command" ? null : "default" as const,
    notify: draft.notify, notify_format: draft.notifyFormat, notify_target: targetOf(draft.notifyTarget),
    success_deadline: draft.deadline.trim() || null,
    alert_failures_after: numberOf(draft.failuresAfter),
    alert_stale_after_seconds: draft.staleAfterMinutes.trim() ? Math.round(Number(draft.staleAfterMinutes) * 60) : null,
    alert_overlap_after: numberOf(draft.overlapAfter),
  };
  if (draft.kind === "command") return {
    ...common, command: draft.command.trim(),
    env: schedule && draft.envText === environmentText(schedule.env ?? {})
      ? schedule.env ?? {} : environmentOf(draft.envText).env,
    env_file: draft.envFile.trim() || null,
  };
  return {
    ...common,
    template_key: draft.templateKey || null,
    template_inputs: draft.templateKey ? draft.templateInputs : {},
    requirement: draft.requirement.trim(),
    run_budget_usd: numberOf(draft.budget),
    profile_overrides: Object.fromEntries(Object.entries(draft.profiles).filter(([, profile]) => profile)),
    task_limits: taskLimitsOf(draft.taskLimits),
    gate_command: draft.gateCommand.trim() || null,
    gate_timeout_seconds: numberOf(draft.gateTimeout),
    daily_budget_usd: numberOf(draft.dailyBudget), user_gates: draft.userGates,
  };
}

const sameProfiles = (a: Partial<Record<TaskRole, string>>, b: Partial<Record<TaskRole, string>>) =>
  ROLES.every((role) => (a[role] || "") === (b[role] || ""));

/** What the fields change, as a PUT takes it; `profile_overrides` and `task_limits` are sent whole, and a cleared
 * field as null. */
function changesOf(fields: FormFields, schedule: ScheduleInfo): ScheduleUpdate {
  const changes: ScheduleUpdate = {};
  const policyKeys = {
    alert_failures_after: "failures_after", alert_stale_after_seconds: "stale_after_seconds", alert_overlap_after: "overlap_after",
  } as const;
  for (const [key, value] of Object.entries(fields)) {
    if (key === "max_parallel_runs" && fields.overlap !== "parallel") continue;
    if (key === "profile_overrides") {
      if (!sameProfiles(fields.profile_overrides ?? {}, schedule.profile_overrides)) changes.profile_overrides = fields.profile_overrides;
      continue;
    }
    if (key === "task_limits") {
      Object.assign(changes, changedTaskLimits(fields.task_limits ?? null, schedule.task_limits));
      continue;
    }
    const stored = key in policyKeys
      ? thresholdValue(schedule, policyKeys[key as keyof typeof policyKeys])
      : key === "notify_format" ? schedule.notify_format ?? "report" : schedule[key as keyof ScheduleInfo] ?? null;
    // Object order does not change saved environment or template inputs.
    const canonical = (item: unknown) => item && typeof item === "object"
      ? JSON.stringify(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item;
    if (canonical(value) !== canonical(stored)) (changes as Record<string, unknown>)[key] = value;
  }
  return changes;
}

/** A new schedule's fields: the optional ones that are empty are left out, so the server's defaults apply. */
function createFields(fields: FormFields, managerId: string, kind: ScheduleKind): ScheduleCreate {
  if (kind === "command") {
    const { work_dir, max_parallel_runs, notify_target, ...rest } = fields;
    return { ...rest, kind, ...(work_dir ? { work_dir } : {}),
      ...(fields.overlap === "parallel" ? { max_parallel_runs } : {}),
      ...(notify_target ? { notify_target } : {}) };
  }
  const {
    template_key,
    template_inputs,
    work_dir,
    max_parallel_runs,
    run_budget_usd,
    profile_overrides,
    task_limits,
    gate_command,
    gate_timeout_seconds,
    daily_budget_usd,
    notify_target,
    success_deadline, alert_failures_after, alert_stale_after_seconds, alert_overlap_after,
    ...rest
  } = fields;
  return {
    ...rest, kind,
    ...(success_deadline ? { success_deadline } : {}),
    ...(alert_failures_after != null ? { alert_failures_after } : {}),
    ...(alert_stale_after_seconds != null ? { alert_stale_after_seconds } : {}),
    ...(alert_overlap_after != null ? { alert_overlap_after } : {}),
    ...(template_key ? { template_key, template_inputs } : {}),
    ...(managerId !== "manager" ? { manager_id: managerId } : {}),
    ...(work_dir ? { work_dir } : {}),
    ...(rest.overlap === "parallel" ? { max_parallel_runs } : {}),
    ...(run_budget_usd != null ? { run_budget_usd } : {}),
    ...(Object.keys(profile_overrides ?? {}).length ? { profile_overrides } : {}),
    ...createTaskLimits(task_limits ?? null),
    ...(gate_command ? { gate_command } : {}),
    ...(gate_timeout_seconds != null ? { gate_timeout_seconds } : {}),
    ...(daily_budget_usd != null ? { daily_budget_usd } : {}),
    ...(notify_target ? { notify_target } : {}),
  };
}

/** The server's check of a rule in a zone, asked once typing pauses; only the latest request's answer counts. Null
 * while there is no rule or its check has not answered yet. */
function useRulePreview(cron: string, timezone: string, gated: boolean) {
  const [state, setState] = useState<{ key: string; preview?: SchedulePreview; error?: string } | null>(null);
  const latest = useRef(0);
  const key = `${cron}\n${timezone}\n${gated}`;
  useEffect(() => {
    const mine = ++latest.current;
    if (!cron) return;
    const timer = setTimeout(() => {
      previewSchedule({ cron, timezone, count: PREVIEW_COUNT, gated })
        .then((preview) => mine === latest.current && setState({ key, preview }))
        .catch((err) => mine === latest.current && setState({ key, error: errorText(err) }));
    }, PREVIEW_DELAY_MS);
    return () => clearTimeout(timer);
  }, [cron, timezone, gated, key]);
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

const linkClass =
  "rounded-sm underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** The gate command, the daily budget and the notifications: the form's "Before each run and after" group. */
function GateBudgetNotify({
  id,
  schedule,
  limits,
  draft,
  set,
  gateLength,
  gateTooLong,
  badGateTimeout,
  badDailyBudget,
}: {
  id: (field: string) => string;
  schedule: ScheduleInfo | null;
  limits: ScheduleLimits;
  draft: Draft;
  set: <K extends keyof Draft>(key: K, value: Draft[K]) => void;
  gateLength: number;
  gateTooLong: boolean;
  badGateTimeout: boolean;
  badDailyBudget: boolean;
}) {
  const targets = limits.notify_targets;
  const chosen = targets.find((target) => targetKey(target) === draft.notifyTarget);
  // An unavailable saved target stays selectable, so the select shows what is saved.
  const gone = draft.notifyTarget && !chosen ? targetOf(draft.notifyTarget) : null;
  const spent = schedule?.spent_today_usd;
  const cap = schedule?.daily_budget_usd;
  const reached = spent != null && cap != null && spent >= cap;
  const channelsLink = (
    <a href={settingsHref("channels")} className={linkClass}>
      Settings → Channels
    </a>
  );

  return (
    <section aria-labelledby={id("around")} className="space-y-3 border-t border-border pt-4">
      <h3 id={id("around")} className="text-xs font-semibold">{draft.kind === "command" ? "Notifications" : "Before each run and after"}</h3>

      {draft.kind === "agent" && <>

      <div className="space-y-1">
        <div className="flex items-baseline justify-between gap-2">
          <label htmlFor={id("gate")} className={LABEL}>Gate command</label>
          <span
            id={id("gate-count")}
            className={cn(
              "flex items-center gap-1 text-[11px] tabular-nums",
              gateTooLong ? "text-destructive" : "text-muted-foreground",
            )}
          >
            {gateTooLong && <AlertCircle className="h-3 w-3" aria-label="Too long" />}
            {gateLength} / {limits.gate_command_max_chars}
          </span>
        </div>
        <Input
          id={id("gate")}
          value={draft.gateCommand}
          onChange={(e) => set("gateCommand", e.target.value)}
          placeholder="python3 scripts/new_alerts.py"
          spellCheck={false}
          autoComplete="off"
          aria-invalid={gateTooLong}
          aria-describedby={`${id("gate-count")} ${id("gate-hint")}`}
          className="font-mono aria-invalid:border-destructive"
        />
        <p id={id("gate-hint")} className="text-xs text-muted-foreground">
          Optional. Runs in the work directory before each run: when it prints nothing the run is skipped, what it
          prints goes to the task, and when it fails or times out the run fails.
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          {/* A block, so it lines up with the budget's label beside it, which shares a row with "Spent today". */}
          <label htmlFor={id("gate-timeout")} className={cn(LABEL, "block")}>Gate timeout (seconds)</label>
          <Input
            id={id("gate-timeout")}
            inputMode="numeric"
            value={draft.gateTimeout}
            onChange={(e) => set("gateTimeout", e.target.value)}
            placeholder={`Default: ${limits.gate_timeout_default_seconds}`}
            aria-invalid={badGateTimeout}
            aria-describedby={id("gate-timeout-hint")}
            className="aria-invalid:border-destructive"
          />
          <p
            id={id("gate-timeout-hint")}
            className={cn("text-xs", badGateTimeout ? "text-destructive" : "text-muted-foreground")}
          >
            {badGateTimeout
              ? `Enter whole seconds from 1 to ${limits.gate_timeout_max_seconds}, or leave it empty.`
              : `Empty: the default, ${interval(limits.gate_timeout_default_seconds)}. At most ${interval(limits.gate_timeout_max_seconds)}.`}
          </p>
        </div>
        <div className="space-y-1">
          <div className="flex items-baseline justify-between gap-2">
            <label htmlFor={id("daily-budget")} className={LABEL}>Daily budget (USD)</label>
            {spent != null && (
              <span
                id={id("spent-today")}
                className={cn(
                  "flex items-center gap-1 text-[11px] tabular-nums",
                  reached ? "text-warning" : "text-muted-foreground",
                )}
              >
                {reached && <AlertCircle className="h-3 w-3" aria-hidden="true" />}
                Spent today: {formatUsd(spent)}
                {reached && " · reached"}
              </span>
            )}
          </div>
          <Input
            id={id("daily-budget")}
            inputMode="decimal"
            value={draft.dailyBudget}
            onChange={(e) => set("dailyBudget", e.target.value)}
            placeholder="No daily cap"
            aria-invalid={badDailyBudget}
            aria-describedby={`${spent != null ? `${id("spent-today")} ` : ""}${id("daily-budget-hint")}`}
            className="aria-invalid:border-destructive"
          />
          <p
            id={id("daily-budget-hint")}
            className={cn("text-xs", badDailyBudget ? "text-destructive" : "text-muted-foreground")}
          >
            {badDailyBudget
              ? "Enter dollars as a number, or leave it empty."
              : "Empty: none. Once this schedule's runs today have spent it, later fires are skipped until midnight, server time."}
          </p>
        </div>
      </div>

      </>}
      <div className="space-y-1">
        <label htmlFor={id("notify-format")} className={LABEL}>Message format</label>
        <select id={id("notify-format")} value={draft.notifyFormat}
          onChange={(e) => set("notifyFormat", e.target.value as ScheduleNotifyFormat)}
          disabled={draft.kind === "command"} aria-describedby={id("notify-format-hint")} className={SELECT}>
          <option value="report">Operator report</option>
          {draft.kind === "agent" && <option value="deliverable">Delivered text only</option>}
        </select>
        <p id={id("notify-format-hint")} className="text-xs text-muted-foreground">
          {draft.kind === "command" ? "Command schedules send operator reports." :
            draft.notifyFormat === "deliverable"
              ? "Sends the delivered text on success and a short reason on failure. Alerts stay on the dashboard."
              : "Includes run status, result summary, cost and dashboard link."}
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Choices
          name={id("notify")}
          legend="Notify"
          value={draft.notify}
          onChange={(value) => set("notify", value)}
          options={scheduleNotifyOptions(draft.kind, Number(draft.failuresAfter || 3))}
        />
        <div className="space-y-1">
          {targets.length === 0 && !gone ? (
            <>
              <p className={LABEL}>Send to</p>
              <p className="text-sm text-foreground">Dashboard only</p>
              <p className="text-xs text-muted-foreground">
                No channel has an allowed sender or a bound chat to send to.{" "}
                <a href={settingsHref("channels")} className={linkClass}>
                  Add a sender or bind a chat in Settings → Channels
                </a>
              </p>
            </>
          ) : (
            <>
              <label htmlFor={id("target")} className={LABEL}>Send to</label>
              <select
                id={id("target")}
                value={draft.notifyTarget}
                onChange={(e) => set("notifyTarget", e.target.value)}
                aria-describedby={id("target-hint")}
                className={SELECT}
              >
                <option value="">Dashboard only</option>
                {targets.map((target) => (
                  <option key={targetKey(target)} value={targetKey(target)}>
                    {`${target.label}${target.channel_enabled ? "" : " (channel off)"}`}
                  </option>
                ))}
                {gone && (
                  <option value={draft.notifyTarget}>{`${gone.channel} · ${gone.chat} (unavailable)`}</option>
                )}
              </select>
              <p id={id("target-hint")} className="text-xs text-muted-foreground">
                {chosen && !chosen.channel_enabled ? (
                  <span className="text-warning">This channel is turned off in {channelsLink}. </span>
                ) : gone ? (
                  <span className="text-warning">
                    This chat is unavailable: its sender allowlist entry or Manager binding was removed ({channelsLink}).{" "}
                  </span>
                ) : null}
                Allowlisted direct chats and chats bound to a Manager. Dashboard only: nothing is sent to a channel.
              </p>
            </>
          )}
        </div>
      </div>
    </section>
  );
}

/**
 * Every field of a schedule, with the live check of its rule. Null `schedule`: a new one, created disabled. The
 * server's refusal (400, 409) is shown as it gives it.
 */
function TaskScheduleForm({
  idPrefix,
  project,
  schedule,
  limits,
  zonesId,
  onDirtyChange,
  onSaved,
  onDeleted,
  onCancel,
  onSubscription,
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
  /** A new schedule's choice of the subscription type, whose form is another. */
  onSubscription?: () => void;
}) {
  const [draft, setDraft] = useState(() => draftOf(schedule, limits));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [managers, setManagers] = useState<ManagerInfo[] | null>(null);
  const [managerError, setManagerError] = useState("");
  const [managerReload, setManagerReload] = useState(0);
  useEffect(() => {
    let active = true;
    setManagerError("");
    fetchManagers().then(
      (rows) => { if (active) setManagers(rows); },
      (err) => { if (active) setManagerError(errorText(err)); },
    );
    return () => { active = false; };
  }, [schedule?.schedule_id, managerReload]);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setSaved(false);
  };
  const owner = managers?.find((manager) => manager.id === draft.managerId);
  const templates = owner?.templates ?? [];
  const template = draft.kind === "agent" ? templates.find((item) => item.key === draft.templateKey) : undefined;
  const showTemplates = Boolean(schedule?.template_key) || managers?.some((manager) => manager.id !== "manager");
  const selectOwner = (managerId: string) => {
    setDraft((d) => ({ ...d, managerId, templateKey: "", templateInputs: {} }));
    setSaved(false);
  };
  const selectTemplate = (templateKey: string) => {
    setDraft((d) => ({ ...d, templateKey, templateInputs: {} }));
    setSaved(false);
  };
  const isCommand = draft.kind === "command";
  const env = environmentOf(draft.envText);
  const fields = fieldsOf(draft, limits, schedule);
  const changes = schedule ? changesOf(fields, schedule) : {};
  const dirty = Object.keys(changes).length > 0;
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);

  const check = useRulePreview(fields.cron, fields.timezone, Boolean(fields.gate_command) || draft.kind === "command");
  const ruleInvalid = check?.preview?.valid === false;
  const missing = [
    !fields.name && "a name",
    !fields.cron && "a rule",
    !isCommand && !fields.requirement && "a requirement",
    isCommand && !fields.command && "a command",
    !fields.work_dir && !template?.work_dir && !project.work_dir && "a work directory",
    !isCommand && owner?.task_policy === "templates_only" && !template && "a task template",
    ...((!isCommand && template?.inputs || []).filter((input) => input.required && !draft.templateInputs[input.key]?.trim())
      .map((input) => input.label || input.key)),
  ].filter(Boolean);
  const tooLong = (fields.requirement ?? "").length > limits.requirement_max_chars;
  const commandTooLong = draft.command.trim().length > 2000;
  const badTimeout = draft.timeoutMode === "limit" && !(Number(fields.run_timeout_seconds) >= 1);
  const badBudget = Number.isNaN(fields.run_budget_usd);
  const gateLength = (fields.gate_command ?? "").length;
  const gateTooLong = gateLength > limits.gate_command_max_chars;
  const gateTimeout = fields.gate_timeout_seconds;
  const badGateTimeout =
    gateTimeout != null &&
    !(Number.isInteger(gateTimeout) && gateTimeout >= 1 && gateTimeout <= limits.gate_timeout_max_seconds);
  const badDailyBudget = Number.isNaN(fields.daily_budget_usd);
  const maxParallel = fields.max_parallel_runs;
  const badMaxParallel =
    fields.overlap === "parallel" &&
    !(Number.isInteger(maxParallel) && maxParallel >= 1 && maxParallel <= limits.max_parallel_runs_limit);
  const badLimit = (role: TaskLimitRole) => badTaskLimit(fields.task_limits?.[role] ?? null, limits.concurrency_max);
  const badLimits = TASK_LIMIT_ROLES.some(badLimit);
  const alertError = [
    fields.success_deadline != null && !/^([01]\d|2[0-3]):[0-5]\d$/.test(fields.success_deadline)
      && "Success deadline must be HH:MM (00:00 through 23:59).",
    fields.alert_failures_after != null && !(Number.isInteger(fields.alert_failures_after) && fields.alert_failures_after >= 1 && fields.alert_failures_after <= 1000)
      && "Enter a whole number of failures from 1 to 1000, or leave it empty.",
    fields.alert_stale_after_seconds != null && !(Number.isInteger(fields.alert_stale_after_seconds) && fields.alert_stale_after_seconds >= 300 && fields.alert_stale_after_seconds <= 2592000)
      && "Enter a no-success window from 5 to 43200 minutes, or leave it empty.",
    fields.alert_overlap_after != null && !(Number.isInteger(fields.alert_overlap_after) && fields.alert_overlap_after >= 0 && fields.alert_overlap_after <= 10000)
      && "Enter a whole number of skipped firings from 0 to 10000, or leave it empty.",
  ].find(Boolean);
  const invalid =
    missing.length > 0 || badMaxParallel || badLimits || tooLong || ruleInvalid || badTimeout || badBudget ||
    gateTooLong || badGateTimeout || badDailyBudget || Boolean(alertError) ||
    (!isCommand && Boolean(draft.templateKey && !template && managers && !managerError)) ||
    (isCommand && (Boolean(env.error) || commandTooLong));
  const id = (field: string) => `${idPrefix}-${field}`;
  // Validation refusals name their field first; match that exact API field.
  const serverField = error.match(/^([a-z_]+)(?:\s|$)/)?.[1];
  const fieldError = (field: string) => serverField === field
    ? <p role="alert" className="text-xs text-destructive">{error}</p> : null;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (invalid || (schedule ? !dirty : !managers || Boolean(managerError))) return;
    setBusy(true);
    setError("");
    try {
      const next = schedule
        ? await updateSchedule(schedule.schedule_id, changes)
        : await createSchedule(project.project_id, createFields(fields, draft.managerId, draft.kind));
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

  return (
    <form onSubmit={submit} className="min-w-0 space-y-4">
      {schedule ? <p className="text-sm text-muted-foreground">Type: {isCommand ? "Command" : "Agent task"}</p> : (
        <Choices name={id("kind")} legend="Type" value={draft.kind}
          onChange={(kind) => {
            if (kind === "subscription") return onSubscription?.();
            setDraft((d) => ({ ...d, kind, notifyFormat: kind === "command" ? "report" : d.notifyFormat, timeoutMode: kind === "command" ? "none" : "default" }));
            setSaved(false);
          }} options={[
            { value: "agent", label: "Agent task", hint: "Each firing starts a task: a Planner and Workers, with model spend." },
            { value: "command", label: "Command", hint: "Each firing runs a shell command directly: no agent, no model spend." },
            ...(onSubscription ? [SUBSCRIPTION_CHOICE] : []),
          ]} />
      )}
      {schedule ? !isCommand && (
        <p className="text-sm text-muted-foreground">
          Owner: {schedule.manager_name || schedule.manager_id}. Ownership cannot be changed.
        </p>
      ) : !isCommand && managers?.some((manager) => manager.id !== "manager") ? (
        <div className="space-y-1">
          <label htmlFor={id("manager")} className={LABEL}>Owner</label>
          <select id={id("manager")} className={SELECT} value={draft.managerId}
            onChange={(e) => selectOwner(e.target.value)}>
            {managers.map((manager) => (
              <option key={manager.id} value={manager.id}>
                {manager.name}{manager.enabled ? "" : " (disabled)"}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">The owner stays with this schedule and its runs.</p>
        </div>
      ) : null}
      {managerError && (
        <div className="space-y-1">
          <p role="alert" className="text-xs text-destructive">Could not load owners: {managerError}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => setManagerReload((value) => value + 1)}>Retry owners</Button>
        </div>
      )}
      {!isCommand && showTemplates && (
        <section aria-label="Template configuration" className="space-y-3">
          <div className="space-y-1">
            <label htmlFor={id("template")} className={LABEL}>Task template{owner?.task_policy === "templates_only" ? " *" : ""}</label>
            <select id={id("template")} className={SELECT} value={draft.templateKey}
              disabled={!managers || Boolean(managerError)}
              onChange={(e) => selectTemplate(e.target.value)} aria-describedby={id("template-hint")}>
              <option value="">{owner?.task_policy === "templates_only" ? "Choose a template" : "No template"}</option>
              {templates.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
              {draft.templateKey && !template && <option value={draft.templateKey}>{schedule?.template_name || draft.templateKey} (unavailable)</option>}
            </select>
            <p id={id("template-hint")} className="text-xs text-muted-foreground">
              {draft.templateKey && !template && managers && !managerError
                ? "This template is unavailable. Choose another template or clear the selection."
                : template?.description || (templates.length ? "Each run uses the owner's current template with these saved inputs." : "This owner has no task templates. Add one in Settings → Managers, or choose another owner.")}
            </p>
            {owner && !owner.enabled && <p className="text-xs text-warning">This owner is disabled. Enable it in Settings → Managers before running this schedule.</p>}
          </div>
          {template?.inputs.map((input) => (
            <div key={input.key} className="space-y-1">
              <label htmlFor={id(`input-${input.key}`)} className={LABEL}>{input.label || input.key}{input.required ? " *" : ""}</label>
              <Input id={id(`input-${input.key}`)} value={draft.templateInputs[input.key] ?? ""}
                onChange={(e) => set("templateInputs", { ...draft.templateInputs, [input.key]: e.target.value })}
                aria-required={input.required} aria-describedby={input.hint ? id(`input-${input.key}-hint`) : undefined}
                autoComplete="off" spellCheck={input.kind !== "url"} inputMode={input.kind === "url" ? "url" : "text"} />
              {input.hint && <p id={id(`input-${input.key}-hint`)} className="text-xs text-muted-foreground">{input.hint}</p>}
            </div>
          ))}
        </section>
      )}
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
          {fieldError("name")}
        </div>
        <div className="space-y-1">
          <label htmlFor={id("dir")} className={LABEL}>Work directory{project.work_dir || template?.work_dir ? "" : " *"}</label>
          <Input
            id={id("dir")}
            value={draft.workDir}
            onChange={(e) => set("workDir", e.target.value)}
            placeholder={template?.work_dir ? `Template: ${template.work_dir}` : project.work_dir ? `Default: ${project.work_dir}` : "Required: the project has no default directory"}
            aria-describedby={id("dir-hint")}
            spellCheck={false}
            className="font-mono"
          />
          {fieldError("work_dir")}
          <p id={id("dir-hint")} className="text-xs text-muted-foreground">
            {template?.work_dir ? "Empty: the template's fixed directory when it fires. " : project.work_dir && "Empty: the project's default directory when it fires. "}
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
          {fieldError("cron")}
          <p id={id("cron-hint")} className="text-xs text-muted-foreground">
            Minute, hour, day of month, month, day of week. Fires at least {interval(isCommand ? limits.min_interval_gated_seconds : limits.min_interval_seconds)} apart{isCommand ? "." : `, or ${interval(limits.min_interval_gated_seconds)} with a gate command.`}
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
          {fieldError("timezone")}
          <p id={id("zone-hint")} className="text-xs text-muted-foreground">
            An IANA name such as Europe/Berlin; the rule is read in it. Empty: {limits.default_timezone}.
          </p>
        </div>
        <div className="sm:col-span-2">
          <RulePreview id={id("preview")} cron={fields.cron} check={check} />
        </div>
      </div>

      {isCommand ? (
        <div className="min-w-0 space-y-3">
          <div className="min-w-0 space-y-1">
            <div className="flex items-baseline justify-between gap-2">
              <label htmlFor={id("command")} className={LABEL}>Command *</label>
              <span className={cn("text-xs tabular-nums", commandTooLong ? "text-destructive" : "text-muted-foreground")}>
                {draft.command.length} / 2000
              </span>
            </div>
            <textarea id={id("command")} value={draft.command} onChange={(e) => set("command", e.target.value)}
              rows={4} spellCheck={false} aria-invalid={commandTooLong || serverField === "command"} className={cn(TEXTAREA, "min-w-0 font-mono whitespace-pre-wrap break-all")} />
            {fieldError("command")}
          </div>
          <div className="space-y-1">
            <label htmlFor={id("env")} className={LABEL}>Environment</label>
            <textarea id={id("env")} value={draft.envText} onChange={(e) => set("envText", e.target.value)}
              rows={4} spellCheck={false} aria-invalid={Boolean(env.error)} aria-describedby={id("env-hint")}
              className={cn(TEXTAREA, "font-mono whitespace-pre-wrap break-all")} />
            {fieldError("env")}
            <p id={id("env-hint")} className="text-xs text-muted-foreground">One KEY=VALUE per line, or a JSON object with string values (use \n for line breaks). Shown as written: put secrets in the env file.</p>
            {env.error && <p role="alert" className="text-xs text-destructive">{env.error}</p>}
            {env.duplicates.length > 0 && <p role="status" className="text-xs text-warning">Duplicate keys: {env.duplicates.join(", ")}; the last value is used.</p>}
          </div>
          <div className="space-y-1">
            <label htmlFor={id("env-file")} className={LABEL}>Env file</label>
            <Input id={id("env-file")} value={draft.envFile} onChange={(e) => set("envFile", e.target.value)}
              spellCheck={false} aria-invalid={serverField === "env_file"} aria-describedby={id("env-file-hint")} className="font-mono" />
            {fieldError("env_file")}
            <p id={id("env-file-hint")} className="text-xs text-muted-foreground">A file of KEY=VALUE lines, read at each run; only its path is stored.</p>
          </div>
        </div>
      ) : <div className="space-y-1">
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
            {(fields.requirement ?? "").length} / {limits.requirement_max_chars}
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
      </div>}

      <div className="grid gap-3 sm:grid-cols-2">
        <Choices
          name={id("overlap")}
          legend="While the previous run is in progress"
          value={draft.overlap}
          onChange={(value) => set("overlap", value)}
          options={[
            { value: "skip", label: "Skip", hint: "Skip this firing while the previous run is in progress." },
            { value: "queue", label: "Queue", hint: "Start it when the previous run ends; at most one waits." },
            {
              value: "parallel",
              label: "Parallel (reentrant)",
              hint: isCommand ? "Start it beside the runs in progress, up to the most below." : "Start it beside the runs in progress, up to the most below. Pre-run checks never run at once.",
            },
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
      {draft.overlap === "parallel" && (
        <div className="space-y-1 sm:max-w-[calc(50%-0.375rem)]">
          <label htmlFor={id("max-parallel")} className={LABEL}>Most runs at once</label>
          <Input
            id={id("max-parallel")}
            inputMode="numeric"
            value={draft.maxParallel}
            onChange={(e) => set("maxParallel", e.target.value)}
            aria-invalid={badMaxParallel}
            aria-describedby={id("max-parallel-hint")}
            className="aria-invalid:border-destructive"
          />
          <p
            id={id("max-parallel-hint")}
            className={cn("text-xs", badMaxParallel ? "text-destructive" : "text-muted-foreground")}
          >
            {badMaxParallel
              ? `A whole number from 1 to ${limits.max_parallel_runs_limit}.`
              : "Runs of this schedule in progress at once; a firing past it is skipped as busy."}
          </p>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-2">
          <Choices
            name={id("timeout-mode")}
            legend="Run timeout"
            value={draft.timeoutMode}
            onChange={(value) => set("timeoutMode", value)}
            options={[
              ...(!isCommand ? [{
                value: "default" as const,
                label: `Use the default (${limits.run_timeout_default_seconds == null ? "no limit" : `${limits.run_timeout_default_seconds} s`})`,
                hint: "Follows Settings → Scheduler. A run keeps the timeout it started with.",
              }] : []),
              { value: "limit", label: "Set a limit", hint: "A run still going after the minutes below is stopped and counts as a failure." },
              { value: "none", label: "No limit", hint: isCommand ? "A run lasts until its command ends." : "A run lasts until its task ends." },
            ]}
          />
          {draft.timeoutMode === "limit" && (
            <div className="space-y-1">
              <label htmlFor={id("timeout")} className={LABEL}>Limit (minutes)</label>
              <Input
                id={id("timeout")}
                inputMode="decimal"
                value={draft.timeout}
                onChange={(e) => set("timeout", e.target.value)}
                aria-invalid={badTimeout}
                aria-describedby={id("timeout-hint")}
                className="aria-invalid:border-destructive"
              />
              <p id={id("timeout-hint")} className={cn("text-xs", badTimeout ? "text-destructive" : "text-muted-foreground")}>
                {badTimeout ? "Enter the minutes as a number of at least 1/60." : "At most 30 days."}
              </p>
            </div>
          )}
        </div>
        {!isCommand && <div className="space-y-1">
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
        </div>}
      </div>

      {!isCommand && <>
      <div className="space-y-1">
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-xs">
          <Switch
            checked={draft.userGates === "on"}
            onChange={(on) => set("userGates", on ? "on" : "off")}
            label="User gates"
          />
          <span className="font-medium">User gates</span>
          <span className="text-muted-foreground">{draft.userGates === "on" ? "On" : "Off"}</span>
        </label>
        <p className="text-xs text-muted-foreground">
          Allow these tasks to wait for your answers when user gates are enabled for the project.
        </p>
        {draft.userGates === "on" && (draft.timeoutMode === "limit" ||
          (draft.timeoutMode === "default" && limits.run_timeout_default_seconds != null)) && (
          <p className="flex items-start gap-1 text-xs text-warning">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            The gate&apos;s wait counts against the run timeout.
          </p>
        )}
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
          {AGENT_PROFILE.plural} tab and Settings → Roles. For a schedule whose runs are alike, a lower-effort Planner
          costs less.
        </p>
      </fieldset>

      <div className="space-y-1">
        <div className="grid gap-3 sm:grid-cols-2">
          {TASK_LIMIT_ROLES.map((role) => (
            <div key={role} className="space-y-1">
              <label htmlFor={id(`limit-${role}`)} className={LABEL}>{TASK_LIMIT_LABELS[role]}</label>
              <Input
                id={id(`limit-${role}`)}
                inputMode="numeric"
                value={draft.taskLimits[role]}
                onChange={(e) => set("taskLimits", { ...draft.taskLimits, [role]: e.target.value })}
                placeholder="Default"
                aria-invalid={badLimit(role)}
                aria-describedby={id("limits-hint")}
                className="aria-invalid:border-destructive"
              />
            </div>
          ))}
        </div>
        <p id={id("limits-hint")} className={cn("text-xs", badLimits ? "text-destructive" : "text-muted-foreground")}>
          {badLimits
            ? `Enter whole numbers from 1 to ${limits.concurrency_max}, or leave them empty.`
            : `Optional. Each run's task gets these as its own limits, 1 to ${limits.concurrency_max}. Empty: the default in Settings → Roles.`}
        </p>
      </div>

      </>}

      <GateBudgetNotify
        id={id}
        schedule={schedule}
        limits={limits}
        draft={draft}
        set={set}
        gateLength={gateLength}
        gateTooLong={gateTooLong}
        badGateTimeout={badGateTimeout}
        badDailyBudget={badDailyBudget}
      />

      <details open={Boolean(schedule?.success_deadline || Object.values(schedule?.alert_policy ?? {}).some((policy) => !policy.default))}
        className="space-y-3 border-t border-border pt-4 text-sm">
        <summary className="w-fit cursor-pointer rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">Alerts</summary>
        {alertError && <p role="alert" className="text-xs text-destructive">{alertError}</p>}
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <label htmlFor={id("deadline")} className={LABEL}>Success deadline</label>
            <Input id={id("deadline")} value={draft.deadline} onChange={(e) => set("deadline", e.target.value)}
              placeholder="HH:MM" pattern="([01][0-9]|2[0-3]):[0-5][0-9]" aria-describedby={id("deadline-hint")} />
            {fieldError("success_deadline")}
            <p id={id("deadline-hint")} className="text-xs text-muted-foreground">Alert once if no run has succeeded since the previous deadline.</p>
          </div>
          <div className="space-y-1">
            <label htmlFor={id("failures-after")} className={LABEL}>Alert after N failures in a row</label>
            <Input id={id("failures-after")} inputMode="numeric" value={draft.failuresAfter}
              onChange={(e) => set("failuresAfter", e.target.value)} placeholder="3" />
            {fieldError("alert_failures_after")}
          </div>
          <div className="space-y-1">
            <label htmlFor={id("stale-after")} className={LABEL}>Alert after no success for (minutes)</label>
            <Input id={id("stale-after")} inputMode="decimal" value={draft.staleAfterMinutes}
              onChange={(e) => set("staleAfterMinutes", e.target.value)} placeholder="3 periods, 15 min – 24 h" />
            {fieldError("alert_stale_after_seconds")}
          </div>
          <div className="space-y-1">
            <label htmlFor={id("overlap-after")} className={LABEL}>Alert after N skipped firings in a row</label>
            <Input id={id("overlap-after")} inputMode="numeric" value={draft.overlapAfter}
              onChange={(e) => set("overlapAfter", e.target.value)} placeholder={isCommand ? "10" : "off"} aria-describedby={id("overlap-after-hint")} />
            {fieldError("alert_overlap_after")}
            <p id={id("overlap-after-hint")} className="text-xs text-muted-foreground">0 turns this alert off.</p>
          </div>
        </div>
      </details>

      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {schedule && <DeleteSchedule schedule={schedule} onDeleted={() => onDeleted?.()} className="mr-auto" />}
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
        <Button type="submit" size="sm" disabled={(schedule ? !dirty : !managers || Boolean(managerError)) || busy || invalid}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
          {schedule ? "Save" : "Create"}
        </Button>
      </div>
    </form>
  );
}

// --- Subscriptions: the stream a schedule of that type reads, and the operator's word on where it pushes ---

const SUBSCRIPTION_CHOICE = {
  value: "subscription" as const,
  label: "Subscription",
  hint: "Each firing reads the new messages of a chat source and has a processor with no tools write from them; "
    + "the output is pushed once you confirm where, and stays here until then.",
};
const CHAT_KIND_OPTIONS: { value: SubscriptionChatKind; label: string }[] = [
  { value: "group", label: "Group chats" }, { value: "topic", label: "Topic groups" },
  { value: "p2p", label: "Direct chats" }, { value: "unknown", label: "Chats of unknown kind" },
];
const SENDER_KIND_OPTIONS: { value: SubscriptionSenderKind; label: string }[] = [
  { value: "user", label: "People" }, { value: "app", label: "Bots" },
];

/** A subscription schedule as its form holds it: text as typed. */
interface SubDraft {
  name: string; cron: string; timezone: string; requirement: string;
  source: string; chatKinds: SubscriptionChatKind[]; senderKinds: SubscriptionSenderKind[];
  excludeMe: boolean; history: boolean; maxAge: string; maxEvents: string; textChars: string;
  quiet: string; header: string; budget: string; dailyBudget: string;
  overlap: ScheduleOverlap; catchUp: ScheduleCatchUp; notify: ScheduleNotify; notifyTarget: string;
  /** What a run writes. "" is a push subscription that predates the field. */
  outputKind: OutputKind;
}

function subDraftOf(schedule: ScheduleInfo | null, limits: ScheduleLimits): SubDraft {
  const sub = schedule?.subscription;
  return {
    name: schedule?.name ?? "", cron: schedule?.cron ?? "", timezone: schedule?.timezone ?? limits.default_timezone,
    requirement: schedule?.requirement ?? "", source: sub?.source_id ?? "",
    chatKinds: sub?.chat_kinds ?? ["group", "topic"], senderKinds: sub?.sender_kinds ?? ["user", "app"],
    excludeMe: sub?.exclude_mentions_me ?? true, history: sub?.include_history ?? false,
    maxAge: String(sub?.max_age_minutes ?? 180), maxEvents: String(sub?.max_events ?? 500),
    textChars: String(sub?.text_max_chars ?? 200), quiet: sub?.quiet_word ?? "SKIP", header: sub?.output_header ?? "",
    budget: schedule?.run_budget_usd == null ? "" : String(schedule.run_budget_usd),
    dailyBudget: schedule?.daily_budget_usd == null ? "" : String(schedule.daily_budget_usd),
    overlap: schedule?.overlap === "queue" ? "queue" : "skip", catchUp: schedule?.catch_up ?? "once",
    notify: schedule?.notify ?? "failures", notifyTarget: targetKey(schedule?.notify_target ?? null),
    outputKind: outputKindOf(sub?.output_kind),
  };
}

/** Fill a new subscription from a preset, keeping only targets supported by its output kind. */
function draftFromPreset(current: SubDraft, fields: SubscriptionPresetFields): SubDraft {
  const sub = fields.subscription;
  const chat = current.notifyTarget.includes("\n") ? current.notifyTarget.slice(current.notifyTarget.indexOf("\n") + 1) : "";
  return {
    ...current,
    name: fields.name, cron: fields.cron, timezone: fields.timezone || current.timezone, requirement: fields.requirement,
    budget: String(fields.run_budget_usd), dailyBudget: fields.daily_budget_usd == null ? "" : String(fields.daily_budget_usd),
    notify: fields.notify, source: sub.source_id, chatKinds: [...sub.chat_kinds], senderKinds: [...sub.sender_kinds],
    ...(sub.exclude_mentions_me === undefined ? {} : { excludeMe: sub.exclude_mentions_me }),
    history: sub.include_history, maxAge: String(sub.max_age_minutes), maxEvents: String(sub.max_events),
    textChars: String(sub.text_max_chars), quiet: sub.quiet_word, header: sub.output_header, outputKind: sub.output_kind,
    notifyTarget: sub.output_kind === "triage" || (sub.output_kind === "intents" && chat && !isDirectChat(chat)) ? "" : current.notifyTarget,
  };
}

/** The schedule's own fields and the subscription's, as a request writes them. */
function subFieldsOf(d: SubDraft, limits: ScheduleLimits) {
  const whole = (text: string) => (/^\d+$/.test(text.trim()) ? Number(text.trim()) : NaN);
  const schedule = {
    name: d.name.trim(), cron: d.cron.trim(), timezone: d.timezone.trim() || limits.default_timezone,
    requirement: d.requirement.trim(), overlap: d.overlap, catch_up: d.catchUp, notify: d.notify,
    notify_target: targetOf(d.notifyTarget), run_budget_usd: numberOf(d.budget), daily_budget_usd: numberOf(d.dailyBudget),
  };
  const subscription: SubscriptionFields = {
    source_id: d.source.trim(), chat_kinds: d.chatKinds, sender_kinds: d.senderKinds,
    exclude_mentions_me: d.excludeMe, include_history: d.history, max_age_minutes: whole(d.maxAge),
    max_events: whole(d.maxEvents), text_max_chars: whole(d.textChars), quiet_word: d.quiet.trim(),
    output_header: d.header.trim(), ...(d.outputKind ? { output_kind: d.outputKind } : {}),
  };
  return { schedule, subscription };
}

const sameValue = (a: unknown, b: unknown) =>
  Array.isArray(a) && Array.isArray(b) ? [...a].sort().join("\n") === [...b].sort().join("\n")
    : JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** What the form changes in a saved subscription schedule, as an update takes it. */
function subChangesOf(fields: ReturnType<typeof subFieldsOf>, saved: ScheduleInfo): ScheduleUpdate {
  const changes: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(fields.schedule))
    if (!sameValue(value, saved[key as keyof ScheduleInfo])) changes[key] = value;
  const stored = saved.subscription;
  const changed = Object.entries(fields.subscription).filter(([key, value]) =>
    !sameValue(value, stored?.[key as keyof SubscriptionInfo]));
  if (changed.length) changes.subscription = Object.fromEntries(changed);
  return changes as ScheduleUpdate;
}

/** The form of a subscription schedule: the stream it reads, the processor's instructions, the caps and where it pushes. */
function SubscriptionForm({
  idPrefix, project, schedule, limits, zonesId, onDirtyChange, onSaved, onDeleted, onCancel, onTask,
}: {
  idPrefix: string;
  project: Project;
  schedule: ScheduleInfo | null;
  limits: ScheduleLimits;
  zonesId: string;
  onDirtyChange?: (dirty: boolean) => void;
  onSaved: (schedule: ScheduleInfo) => void;
  onDeleted?: () => void;
  onCancel?: () => void;
  /** A new schedule's choice of another type. */
  onTask?: () => void;
}) {
  const [draft, setDraft] = useState(() => subDraftOf(schedule, limits));
  const [triageEnabled, setTriageEnabled] = useState(false);
  useEffect(() => {
    if (schedule) return;
    let alive = true;
    callV1<{ enabled: boolean }>("/api/v1/triage_settings/get", {}).then((result) => {
      if (alive) setTriageEnabled(!("status" in result) && result.enabled);
    }).catch(() => { if (alive) setTriageEnabled(false); });
    return () => { alive = false; };
  }, [schedule]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const set = <K extends keyof SubDraft>(key: K, value: SubDraft[K]) => { setDraft((d) => ({ ...d, [key]: value })); setSaved(false); };
  const toggle = <T extends string>(list: T[], value: T): T[] => list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
  const fields = subFieldsOf(draft, limits);
  const changes = schedule ? subChangesOf(fields, schedule) : {};
  const dirty = Object.keys(changes).length > 0;
  useEffect(() => onDirtyChange?.(dirty), [dirty, onDirtyChange]);
  const check = useRulePreview(fields.schedule.cron, fields.schedule.timezone, true);
  const sub = fields.subscription;
  const inRange = (value: number | undefined, low: number, high: number) => Number.isInteger(value) && value! >= low && value! <= high;
  const tooLong = fields.schedule.requirement.length > limits.requirement_max_chars;
  const problems = [
    !fields.schedule.name && "a name", !fields.schedule.cron && "a rule", !fields.schedule.requirement && "instructions",
    !sub.source_id && "a source", !sub.chat_kinds?.length && "a kind of chat", !sub.sender_kinds?.length && "a kind of sender",
    !(fields.schedule.run_budget_usd! > 0) && "a spend cap per run",
    !inRange(sub.max_age_minutes, 5, 10080) && "a look-back of 5 to 10080 minutes",
    !inRange(sub.max_events, 1, 2000) && "a message cap of 1 to 2000", !inRange(sub.text_max_chars, 20, 4000) && "a text length of 20 to 4000",
    !sub.quiet_word || /\s/.test(sub.quiet_word) ? "a quiet word of one word" : false,
    Number.isNaN(fields.schedule.daily_budget_usd) && "a daily budget as a number",
  ].filter(Boolean);
  const invalid = problems.length > 0 || tooLong || check?.preview?.valid === false;
  const id = (field: string) => `${idPrefix}-${field}`;
  const targets = limits.notify_targets;
  const shownTargets = draft.outputKind === "intents" ? targets.filter((target) => isDirectChat(target.chat)) : targets;
  const gone = draft.notifyTarget && !shownTargets.some((t) => targetKey(t) === draft.notifyTarget) ? targetOf(draft.notifyTarget) : null;
  const applyPreset = async (outputKind: "leads" | "intents" | "triage") => {
    setBusy(true);
    setError("");
    try {
      const { fields: preset } = await subscriptionPreset({ output_kind: outputKind });
      setDraft((current) => draftFromPreset(current, preset));
      setSaved(false);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (invalid || (schedule && !dirty)) return;
    setBusy(true);
    setError("");
    try {
      const { notify_target, run_budget_usd, daily_budget_usd, ...rest } = fields.schedule;
      const next = schedule
        ? await updateSchedule(schedule.schedule_id, changes)
        : await createSchedule(project.project_id, {
          ...rest, kind: "subscription", run_budget_usd, subscription: fields.subscription,
          ...(daily_budget_usd != null ? { daily_budget_usd } : {}), ...(notify_target ? { notify_target } : {}),
        });
      if (schedule) { setDraft(subDraftOf(next, limits)); setSaved(true); }
      onSaved(next);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="min-w-0 space-y-4">
      {schedule ? <p className="text-sm text-muted-foreground">Type: Subscription</p> : (
        <Choices name={id("kind")} legend="Type" value="subscription" onChange={(kind) => { if (kind !== "subscription") onTask?.(); }}
          options={[
            { value: "agent" as ScheduleKind, label: "Agent task", hint: "Each firing starts a task: a Planner and Workers, with model spend." },
            { value: "command" as ScheduleKind, label: "Command", hint: "Each firing runs a shell command directly: no agent, no model spend." },
            SUBSCRIPTION_CHOICE,
          ]} />
      )}
      {!schedule && (
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void applyPreset("leads")}>消息线索</Button>
          <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void applyPreset("intents")}>意图识别</Button>
          {triageEnabled && <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void applyPreset("triage")}>事件分流</Button>}
        </div>
      )}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor={id("name")} className={LABEL}>Name *</label>
          <Input id={id("name")} value={draft.name} maxLength={limits.name_max_chars} onChange={(e) => set("name", e.target.value)} />
        </div>
        <div className="space-y-1">
          <label htmlFor={id("source")} className={LABEL}>Message source *</label>
          <Input id={id("source")} value={draft.source} onChange={(e) => set("source", e.target.value)} spellCheck={false} autoComplete="off" />
          <p className="text-xs text-muted-foreground">The id of an event source: the stream whose new messages each run reads.</p>
        </div>
        <div className="space-y-1">
          <label htmlFor={id("cron")} className={LABEL}>Rule *</label>
          <Input id={id("cron")} value={draft.cron} onChange={(e) => set("cron", e.target.value)} placeholder="5 * * * *" className="font-mono" spellCheck={false} />
        </div>
        <div className="space-y-1">
          <label htmlFor={id("zone")} className={LABEL}>Time zone</label>
          <Input id={id("zone")} value={draft.timezone} onChange={(e) => set("timezone", e.target.value)} list={zonesId} spellCheck={false} autoComplete="off" />
          <p className="text-xs text-muted-foreground">The rule is read in it, and so are the times the processor reads.</p>
        </div>
        <div className="sm:col-span-2"><RulePreview id={id("preview")} cron={fields.schedule.cron} check={check} /></div>
      </div>

      <div className="space-y-1">
        <div className="flex items-baseline justify-between gap-2">
          <label htmlFor={id("requirement")} className={LABEL}>Processor instructions *</label>
          <span className={cn("text-[11px] tabular-nums", tooLong ? "text-destructive" : "text-muted-foreground")}>
            {fields.schedule.requirement.length} / {limits.requirement_max_chars}
          </span>
        </div>
        <textarea id={id("requirement")} value={draft.requirement} onChange={(e) => set("requirement", e.target.value)} rows={5}
          aria-invalid={tooLong} className={TEXTAREA} />
        <p className="text-xs text-muted-foreground">
          What the processor writes from the messages. It has no tools and reads the messages as data; its Agent Profile is the
          processor&apos;s in Settings → Roles.
        </p>
      </div>

      <fieldset className="space-y-2 border-t border-border pt-4">
        <legend className={cn(LABEL, "mb-1")}>What a run reads</legend>
        {draft.outputKind === "triage" ? (
          <p className="text-xs text-muted-foreground">固定范围：私聊、@你的消息、你自己发的消息和已登记的告警机器人；不读导入历史和 Looper 自己的应用。</p>
        ) : <>
        <div className="flex flex-wrap gap-x-5 gap-y-1">
          {CHAT_KIND_OPTIONS.map((option) => (
            <label key={option.value} className="flex min-h-11 items-center gap-2 text-xs sm:min-h-0">
              <input type="checkbox" className="h-4 w-4 accent-primary" checked={draft.chatKinds.includes(option.value)}
                onChange={() => set("chatKinds", toggle(draft.chatKinds, option.value))} />{option.label}
            </label>))}
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1">
          {SENDER_KIND_OPTIONS.map((option) => (
            <label key={option.value} className="flex min-h-11 items-center gap-2 text-xs sm:min-h-0">
              <input type="checkbox" className="h-4 w-4 accent-primary" checked={draft.senderKinds.includes(option.value)}
                onChange={() => set("senderKinds", toggle(draft.senderKinds, option.value))} />{option.label}
            </label>))}
          <label className="flex min-h-11 items-center gap-2 text-xs sm:min-h-0">
            <input type="checkbox" className="h-4 w-4 accent-primary" checked={draft.excludeMe}
              onChange={(e) => set("excludeMe", e.target.checked)} />Leave out messages that @-mention me</label>
          <label className="flex min-h-11 items-center gap-2 text-xs sm:min-h-0">
            <input type="checkbox" className="h-4 w-4 accent-primary" checked={draft.history}
              onChange={(e) => set("history", e.target.checked)} />Include history and imports</label>
        </div>
        </>}
        <div className="grid gap-3 sm:grid-cols-3">
          {([["maxAge", "Look back (minutes)", "Older messages are dropped and counted."], ["maxEvents", "Newest messages (at most)", "More are cut, oldest first."],
            ["textChars", "Characters per message", "Each message is cut to this many."]] as const).map(([key, label, hint]) => (
            <div key={key} className="space-y-1">
              <label htmlFor={id(key)} className={LABEL}>{key === "maxEvents" && draft.outputKind === "triage" ? "每次最多消息数" : label}</label>
              <Input id={id(key)} inputMode="numeric" value={draft[key]} onChange={(e) => set(key, e.target.value)} />
              <p className="text-xs text-muted-foreground">{key === "maxEvents" && draft.outputKind === "triage" ? "按最旧消息优先读取，读不完的下一次继续。" : hint}</p>
            </div>))}
        </div>
      </fieldset>

      <fieldset className="space-y-2 border-t border-border pt-4">
        <legend className={cn(LABEL, "mb-1")}>{draft.outputKind === "triage" ? "结果与预算" : "What it pushes"}</legend>
        <div className="grid gap-3 sm:grid-cols-2">
          {draft.outputKind !== "triage" && <>
          <div className="space-y-1">
            <label htmlFor={id("quiet")} className={LABEL}>Quiet word</label>
            <Input id={id("quiet")} value={draft.quiet} onChange={(e) => set("quiet", e.target.value)} spellCheck={false} />
            <p className="text-xs text-muted-foreground">A reply that is only this word, or nothing, is not pushed.</p>
          </div>
          <div className="space-y-1">
            <label htmlFor={id("header")} className={LABEL}>Title</label>
            <Input id={id("header")} value={draft.header} onChange={(e) => set("header", e.target.value)} />
            <p className="text-xs text-muted-foreground">Written above the reply; @ marks in the reply are removed.</p>
          </div>
          </>}
          <div className="space-y-1">
            <label htmlFor={id("budget")} className={LABEL}>Spend cap per run (USD) *</label>
            <Input id={id("budget")} inputMode="decimal" value={draft.budget} onChange={(e) => set("budget", e.target.value)} />
          </div>
          <div className="space-y-1">
            <label htmlFor={id("daily")} className={LABEL}>Daily budget (USD)</label>
            <Input id={id("daily")} inputMode="decimal" value={draft.dailyBudget} onChange={(e) => set("dailyBudget", e.target.value)} placeholder="No daily cap" />
          </div>
          <div className="space-y-1">
            {draft.outputKind === "triage" ? <p className="text-xs text-muted-foreground">结果仅存储，暂不发送；重要事件等待管家交接，摘要等待晨报接入。</p> : <>
            <label htmlFor={id("target")} className={LABEL}>Push to</label>
            <select id={id("target")} value={draft.notifyTarget} onChange={(e) => set("notifyTarget", e.target.value)} className={SELECT}>
              <option value="">Dashboard only</option>
              {shownTargets.map((target) => (
                <option key={targetKey(target)} value={targetKey(target)}>{`${target.label}${target.channel_enabled ? "" : " (channel off)"}`}</option>))}
              {gone && <option value={draft.notifyTarget}>{`${gone.channel} · ${gone.chat} (unavailable)`}</option>}
            </select>
            {draft.outputKind === "intents" && <p className="text-xs text-muted-foreground">意图识别只推到私聊。</p>}
            <p className="text-xs text-muted-foreground">
              A direct chat or a bound group. Changing it, the rule or the zone, or turning the subscription on, asks you to
              confirm before anything is pushed; until then the output stays on this page.
            </p>
            </>}
          </div>
          <Choices name={id("notify")} legend="Failures" value={draft.notify} onChange={(value) => set("notify", value)}
            options={scheduleNotifyOptions("agent", 3).filter((option) => option.value !== "always")} />
        </div>
        <Choices name={id("overlap")} legend="While the previous run is in progress" value={draft.overlap}
          onChange={(value) => set("overlap", value)} options={[
            { value: "skip", label: "Skip", hint: "Skip this firing; its messages are read by the next run." },
            { value: "queue", label: "Queue", hint: "Start it when the previous run ends; at most one waits." },
          ]} />
      </fieldset>

      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {schedule && <DeleteSchedule schedule={schedule} onDeleted={() => onDeleted?.()} className="mr-auto" />}
        {saved && !dirty && <span role="status" className="flex items-center gap-1 text-xs text-success"><CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />Saved</span>}
        {problems.length > 0 && (dirty || !schedule) && <span className="text-xs text-muted-foreground">Needs {problems.join(", ")}</span>}
        {schedule ? (
          <Button type="button" variant="outline" size="sm" disabled={!dirty || busy}
            onClick={() => { setDraft(subDraftOf(schedule, limits)); setError(""); }}>Discard changes</Button>
        ) : <Button type="button" variant="ghost" size="sm" onClick={onCancel} disabled={busy}>Cancel</Button>}
        <Button type="submit" size="sm" disabled={(schedule ? !dirty : false) || busy || invalid}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}{schedule ? "Save" : "Create"}
        </Button>
      </div>
    </form>
  );
}

/** A schedule's form: the subscription's own, or the task and command one. */
function ScheduleForm(props: Omit<React.ComponentProps<typeof TaskScheduleForm>, "onSubscription">) {
  const [subscription, setSubscription] = useState(props.schedule?.kind === "subscription");
  return subscription
    ? <SubscriptionForm {...props} onTask={props.schedule ? undefined : () => setSubscription(false)} />
    : <TaskScheduleForm {...props} onSubscription={props.schedule ? undefined : () => setSubscription(true)} />;
}

/**
 * Where a subscription pushes and whether the operator has said it may: until they confirm the target, what its runs
 * make stays on this page. "Confirm push target" shows what confirming means (the server's own text: the target and
 * the bot, the rule and its next fires, how often at most, the processor's Profile and where it comes from, the
 * machine's managed hooks, the caps) and confirms it; "Reset cursor" moves where the next run starts reading.
 */
export function SubscriptionStatus({ schedule, onChange }: { schedule: ScheduleInfo; onChange: (schedule: ScheduleInfo) => void }) {
  const sub = schedule.subscription;
  const id = useId();
  const [panel, setPanel] = useState<"" | "confirm" | "reset">("");
  const [text, setText] = useState<string | null>(null);
  /** The version of the schedule the panel shows: the confirmation names it. */
  const [version, setVersion] = useState("");
  const [after, setAfter] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [moved, setMoved] = useState("");
  if (!sub) return null;
  const triage = sub.output_kind === "triage";
  const target = schedule.notify_target;
  const close = () => { setPanel(""); setError(""); };
  const openConfirm = async () => {
    setPanel("confirm"); setText(null); setError("");
    try {
      const shown = (await fetchSchedule(schedule.schedule_id)).subscription;
      setVersion(shown?.config_version ?? "");
      setText(shown?.confirmation ?? "");
    } catch (err) { setError(errorText(err)); }
  };
  const confirm = async () => {
    setBusy(true); setError("");
    try { onChange(await confirmSubscriptionTarget(schedule.schedule_id, version)); close(); }
    catch (err) {
      // It changed after it was shown (another tab, another request): what it is now replaces the panel.
      setError(errorText(err));
      void fetchSchedule(schedule.schedule_id).then((now) => { onChange(now); setText(null); setPanel(""); }, () => {});
    }
    finally { setBusy(false); }
  };
  const reset = async () => {
    const moment = new Date(after);
    if (!after || Number.isNaN(moment.getTime())) { setError("Choose the time after which events are read."); return; }
    setBusy(true); setError("");
    try {
      const done = await resetSubscriptionCursor(schedule.schedule_id, moment.toISOString());
      onChange(done.schedule);
      setMoved(`Cursor at ${done.cursor.cursor_seq}: the next run reads the ${done.cursor.events_after} events stored since.`);
      close();
    } catch (err) { setError(errorText(err)); }
    finally { setBusy(false); }
  };
  return (
    <div className="basis-full space-y-2 text-xs">
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-muted-foreground">
        {triage ? (sub.target_confirmed_at
          ? <><CheckCircle2 className="h-3.5 w-3.5 text-success" aria-hidden="true" />事件分流已确认：结果仅存储，暂不发送。</>
          : <><AlertCircle className="h-3.5 w-3.5 text-warning" aria-hidden="true" /><span className="text-warning">事件分流尚未确认：确认后才读取消息。</span></>)
          : target ? (sub.target_confirmed_at
          ? <><CheckCircle2 className="h-3.5 w-3.5 text-success" aria-hidden="true" />Pushes to {target.channel} · {target.chat}, confirmed {formatInZone(sub.target_confirmed_at, schedule.timezone)}</>
          : <><AlertCircle className="h-3.5 w-3.5 text-warning" aria-hidden="true" /><span className="text-warning">Push target not confirmed: output stays on this page.</span></>)
          : "Dashboard only: nothing is pushed."}
        <span className="tabular-nums">Cursor {sub.cursor_seq}</span>
        {!!sub.rules?.length && <span title={sub.rules.map((rule) => rule.label || rule.name).join(", ")}>
          Selects by {sub.rules.length} rule{sub.rules.length === 1 ? "" : "s"} (set through the API)</span>}
        {!!sub.uncertain_deliveries && <span className="text-warning" role="status">
          {sub.uncertain_deliveries} message{sub.uncertain_deliveries === 1 ? "" : "s"} of the last 7 days may not have been sent
          (cut off while sending; never sent again)</span>}
        {(triage || target) && !sub.target_confirmed_at && panel !== "confirm" && (
          <Button type="button" variant="outline" size="sm" onClick={openConfirm} className="h-11 sm:h-7">{triage ? "确认事件分流…" : "Confirm push target…"}</Button>)}
        {panel !== "reset" && <Button type="button" variant="ghost" size="sm" onClick={() => { setPanel("reset"); setMoved(""); }} className="h-11 sm:h-7">Reset cursor…</Button>}
      </p>
      {moved && <p role="status" className="text-muted-foreground">{moved}</p>}
      {panel === "confirm" && (
        <section role="group" aria-labelledby={`${id}-confirm`} className="space-y-2 rounded-md border border-warning/50 bg-muted/20 p-3">
          <h4 id={`${id}-confirm`} className="font-semibold">{triage ? "确认事件分流" : "Confirm the push target"}</h4>
          {text == null && !error ? <Loader2 className="h-4 w-4 animate-spin" aria-label="Loading" />
            : <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words font-sans text-xs leading-relaxed">{text}</pre>}
          {error && <p role="alert" className="text-destructive">{error}</p>}
          <div className="flex gap-2">
            <Button type="button" size="sm" onClick={confirm} disabled={busy || !text} className="h-11 sm:h-8">
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}Confirm</Button>
            <Button type="button" variant="ghost" size="sm" onClick={close} disabled={busy} className="h-11 sm:h-8">Cancel</Button>
          </div>
        </section>)}
      {panel === "reset" && (
        <section role="group" aria-labelledby={`${id}-reset`} className="space-y-2 rounded-md border border-border bg-muted/20 p-3">
          <h4 id={`${id}-reset`} className="font-semibold">Reset the cursor</h4>
          <label htmlFor={`${id}-after`} className={LABEL}>Read the events stored after</label>
          <Input id={`${id}-after`} type="datetime-local" value={after} onChange={(e) => setAfter(e.target.value)} className="max-w-64" />
          <p className="text-muted-foreground">Your local time. The next run reads exactly the events this stream stored after it, then the subscription&apos;s own limits apply. Refused while a run is in progress.</p>
          {error && <p role="alert" className="text-destructive">{error}</p>}
          <div className="flex gap-2">
            <Button type="button" size="sm" onClick={reset} disabled={busy} className="h-11 sm:h-8">
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}Reset</Button>
            <Button type="button" variant="ghost" size="sm" onClick={close} disabled={busy} className="h-11 sm:h-8">Cancel</Button>
          </div>
        </section>)}
    </div>
  );
}

const OUTCOME_LABELS: Record<SubscriptionRunInfo["outcome"], string> = {
  pending: "Reading", delivering: "Pushing", delivered: "Pushed", uncertain: "Pushed, some messages uncertain", quiet: "Nothing to say", dashboard_only: "Kept here (target not confirmed)",
  output_invalid: "Reply unusable", failed: "Not pushed", extracted: "Extracted", skipped: "Skipped",
};
const SKIP_LABELS: Record<NonNullable<SubscriptionRunInfo["skip_reason"]>, string> = {
  blocked_by_hook: "a prompt hook blocked it", invalid_reply: "its reply could not be read, twice",
};

function triageCountLine(counts: NonNullable<SubscriptionRunInfo["extract_counts"]>): string | null {
  if (typeof counts.important !== "number") return null;
  const dropped = counts.dropped ? Object.values(counts.dropped).reduce((sum, value) => sum + value, 0) : 0;
  return `important ${counts.important} · digest ${counts.digest ?? 0} · ignore ${counts.ignore ?? 0} · uncited ${counts.uncited ?? 0} · raised ${counts.raised ?? 0} · dropped ${dropped}`;
}

/** What a subscription run read and made, under its line of the history. */
function SubscriptionRun({ run }: { run: ScheduleRunInfo }) {
  const sub = run.subscription;
  if (!sub) return null;
  const dropped = [sub.events_too_old && `${sub.events_too_old} too old`, sub.events_over_cap && `${sub.events_over_cap} over the cap`].filter(Boolean);
  const counts = sub.extract_counts ? triageCountLine(sub.extract_counts) : null;
  return (
    <div className="min-w-0 text-xs sm:basis-full">
      <p className="text-muted-foreground">
        <span className="font-medium text-foreground">{OUTCOME_LABELS[sub.outcome]}{sub.skip_reason && ` (${SKIP_LABELS[sub.skip_reason]})`}</span>
        {" · "}{sub.events_selected} of {sub.events_in_window} messages read{dropped.length > 0 && ` (${dropped.join(", ")})`}
        {" · "}events {sub.from_seq + 1}–{sub.to_seq}
        {sub.deliveries && sub.deliveries.length > 0 && ` · messages: ${sub.deliveries.map((d) => `${d.segment} ${d.state}`).join(", ")}`}
      </p>
      {counts && <p className="text-muted-foreground">{counts}</p>}
      {sub.output_text && (
        <details className="mt-1">
          <summary className="w-fit cursor-pointer rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">Output</summary>
          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/20 px-3 py-2 font-sans text-[11px] text-foreground/90">{sub.output_text}</pre>
        </details>)}
    </div>
  );
}

/**
 * "Delete schedule", then the inline confirmation ("Delete <name>? Its tasks stay."): it deletes the schedule and its
 * run history, the tasks it created staying; `onDeleted` runs once it is gone. A run in progress keeps its task
 * running. The project's Schedules tab, the Schedules page and a schedule's own page share it.
 */
export function DeleteSchedule({
  schedule,
  onDeleted,
  className,
}: {
  schedule: ScheduleInfo;
  onDeleted: () => void;
  className?: string;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const remove = async () => {
    setBusy(true);
    setError("");
    try {
      await deleteSchedule(schedule.schedule_id);
      onDeleted();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
    }
  };

  if (!confirming)
    return (
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setConfirming(true)}
        aria-label={`Delete schedule: ${schedule.name}`}
        // A narrow screen shows the icon alone, a 44 px target, so a row's actions stay on one line.
        className={cn(
          "h-11 w-11 px-0 text-destructive hover:bg-destructive/10 hover:text-destructive sm:h-8 sm:w-auto sm:px-3",
          className,
        )}
      >
        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
        <span className="hidden sm:inline">Delete schedule</span>
      </Button>
    );
  return (
    <span className={cn("flex min-w-0 flex-wrap items-center gap-2", className)}>
      <span className="min-w-0 break-words text-xs text-muted-foreground">Delete {schedule.name}? Its tasks stay.</span>
      <span className="flex items-center gap-2">
        <Button type="button" variant="destructive" size="sm" onClick={remove} disabled={busy} className="h-11 sm:h-8">
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          Delete
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setConfirming(false);
            setError("");
          }}
          disabled={busy}
          className="h-11 sm:h-8"
        >
          Cancel
        </Button>
      </span>
      {error && (
        <span role="alert" className="basis-full text-xs text-destructive">
          Could not delete it: {error}
        </span>
      )}
    </span>
  );
}

// --- Run history ---

/** How many runs a page of the history holds, and the most one request may ask for. */
const RUNS_PAGE = 20;
const RUNS_MAX = 200;

/** How long a finished run took: "45 s", "3 min 05 s", "1 h 20 min". */
function durationText(run: ScheduleRunInfo): string | null {
  // A run that counts repeated skips spans its firings ("×N, until"); it took no time of its own.
  if (run.firings > 1) return null;
  const value = run.duration_seconds ?? (run.fired_at && run.finished_at
    ? (Date.parse(run.finished_at) - Date.parse(run.fired_at)) / 1000 : null);
  if (value == null) return null;
  return secondsText(value);
}

function secondsText(value: number): string | null {
  const seconds = Math.max(0, Math.round(value));
  if (!Number.isFinite(seconds)) return null;
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
  return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min`;
}

function runStatsText(stats: RunStats, label: string) {
  if (!stats.runs) return `No runs in the ${label === "Last 24 h" ? "last 24 h" : "last 7 days"}`;
  return `${label}: ${stats.succeeded}/${stats.runs} succeeded · avg ${stats.avg_seconds == null ? "—" : secondsText(stats.avg_seconds)} · max ${stats.max_seconds == null ? "—" : secondsText(stats.max_seconds)}`;
}

/** Text decoding belongs only to the preview; downloads retain the original bytes. */
function CommandRunLog({ run }: { run: ScheduleRunInfo }) {
  const [tail, setTail] = useState<string | null>(null);
  const [partialLines, setPartialLines] = useState<number | null>(null);
  const [complete, setComplete] = useState(!isActiveRun(run));
  const [truncated, setTruncated] = useState(run.log?.truncated ?? false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [downloadError, setDownloadError] = useState("");
  const [progress, setProgress] = useState<{ downloaded: number; total: number } | null>(null);

  const active = isActiveRun(run);
  const hasLog = run.log != null && (active || run.log.bytes !== 0 || run.started_at != null);
  const latest = useRef(0);
  const refresh = useCallback(async () => {
    if (!hasLog) return;
    const mine = ++latest.current;
    setLoading(true);
    setError("");
    try {
      const log = await fetchRunLogTail(run.run_id);
      if (mine !== latest.current) return;
      setTail(log.text);
      setPartialLines(log.partialLines);
      setComplete(log.complete);
      setTruncated(log.truncated);
    } catch (err) {
      if (mine === latest.current) setError(errorText(err));
    } finally {
      if (mine === latest.current) setLoading(false);
    }
  }, [run.run_id, hasLog]);

  useEffect(() => {
    void refresh();
    return () => { latest.current++; };
  }, [refresh, active]);

  const download = async () => {
    setProgress({ downloaded: 0, total: run.log?.bytes ?? 0 });
    setDownloadError("");
    try {
      const blob = await downloadRunLog(run.run_id, (downloaded, total) => setProgress({ downloaded, total }));
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${run.run_id}.log`;
      document.body.append(link);
      link.click();
      link.remove();
      // Let the browser start reading the Blob before releasing its URL.
      setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (err) {
      setDownloadError(errorText(err));
    } finally {
      setProgress(null);
    }
  };

  if (!hasLog) return (
    <div className="space-y-2 pb-3 text-xs text-muted-foreground">
      {(run.error ?? run.summary) && <p className="break-words">{run.error ?? run.summary}</p>}
      <p>This run did not start the command, so it has no log.</p>
    </div>
  );

  return (
    <div className="min-w-0 space-y-2 pb-3 text-xs">
      {(run.error ?? run.summary) && <p className="break-words text-muted-foreground">{run.error ?? run.summary}</p>}
      <pre aria-label="Run log" className="max-h-64 overflow-auto whitespace-pre rounded-md border border-border bg-muted/20 px-3 py-2 font-mono text-[11px]">
        {tail ?? run.log_tail ?? (loading ? "Loading log…" : "")}
      </pre>
      {partialLines != null && <p className="text-muted-foreground">Showing the last {partialLines} lines; download the full log for the rest.</p>}
      {error && <p role="alert" className="text-destructive">{error}</p>}
      {(truncated || run.log?.truncated) && <p className="text-warning">The log was truncated at 50 MiB; the middle is left out.</p>}
      {(active && !complete) && <p className="text-muted-foreground">Still running: the log grows</p>}
      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" size="sm" variant="outline" onClick={refresh} disabled={loading} className="h-11 sm:h-8">
          {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          Refresh log
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={download} disabled={progress !== null}
          className="min-h-11 max-w-full whitespace-normal break-words sm:min-h-8">
          {progress ? `Downloading ${progress.downloaded.toLocaleString()} / ${progress.total.toLocaleString()} bytes` : "Download log"}
        </Button>
      </div>
      {downloadError && <p role="alert" className="text-destructive">{downloadError}</p>}
    </div>
  );
}

function CommandRun({ run, timeZone, linked }: { run: ScheduleRunInfo; timeZone: string; linked: boolean }) {
  const [expanded, setExpanded] = useState(linked);
  const row = useRef<HTMLLIElement>(null);
  const id = useId();
  useEffect(() => {
    if (!linked) return;
    setExpanded(true);
    row.current?.scrollIntoView({ block: "center" });
  }, [linked]);
  const fields = [
    ["Status", <span className="flex flex-wrap items-center gap-1"><RunStatusBadge status={run.status} />
      {(run.exit_signal != null || run.exit_code != null) && <span>
        {run.exit_signal != null ? `signal ${run.exit_signal}` : `exit ${run.exit_code}`}
      </span>}</span>],
    ["Scheduled", formatInZone(run.scheduled_for, timeZone)],
    ["Started", formatInZone(run.started_at, timeZone)],
    ["Finished", formatInZone(run.finished_at, timeZone)],
    ["Duration", !run.started_at || ["skipped_overlap", "skipped_missed", "skipped_gate", "skipped_budget", "skipped_busy"].includes(run.status) ? "—" : durationText(run) ?? "—"],
    ["Trigger", triggerText(run)],
  ] as const;
  return (
    <li ref={row} data-run-id={run.run_id} className="min-w-0">
      <button type="button" aria-expanded={expanded} aria-controls={`${id}-log`} onClick={() => setExpanded((value) => !value)}
        className="grid w-full grid-cols-2 gap-3 rounded-sm py-3 text-left text-xs hover:bg-muted/20 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring sm:grid-cols-3 xl:grid-cols-6">
        {fields.map(([label, value]) => <span key={label} className="min-w-0 break-words">
          <span className="mb-1 flex items-center gap-1 text-muted-foreground">
            {label === "Status" && (expanded ? <ChevronDown className="h-3 w-3" aria-hidden="true" /> : <ChevronRight className="h-3 w-3" aria-hidden="true" />)}
            {label}
          </span>{value}
        </span>)}
      </button>
      {expanded && <div id={`${id}-log`}><CommandRunLog run={run} /></div>}
    </li>
  );
}

/**
 * A schedule's runs, newest first, a page at a time ("Show older runs"), the routine skips (nothing new, busy) left
 * out unless asked for. Each run: when it was due, how it fired, its status, its task (title, status, link), how long
 * it took, its cost, its error or summary, and the gate's output. Loaded when shown first, after "Run now"
 * (`version`) and when the newest run changes, and every 10 s while `open` with a run going.
 */
export function RunHistory({ schedule, open, version, runId = "" }: {
  schedule: ScheduleInfo; open: boolean; version: number; runId?: string;
}) {
  const id = useId();
  const [page, setPage] = useState<ScheduleRunsPage | null>(null);
  const [error, setError] = useState("");
  const [hideSkips, setHideSkips] = useState(!runId);
  const [status, setStatus] = useState<ScheduleRunStatus | "">("");
  const [loadingMore, setLoadingMore] = useState(false);
  const latest = useRef(0);
  /** How many runs are shown, so a refresh keeps the pages already loaded. */
  const shown = useRef(RUNS_PAGE);
  const scheduleId = schedule.schedule_id;
  const isCommand = schedule.kind === "command";
  // A run that counts repeated skips changes by its count.
  const newest = schedule.last_run
    ? `${schedule.last_run.run_id}:${schedule.last_run.status}:${schedule.last_run.firings}`
    : "";
  const skips = status ? "show" : hideSkips ? "hide" : "show";

  useEffect(() => {
    if (runId) {
      shown.current = RUNS_PAGE;
      setHideSkips(false);
      setStatus("");
    }
  }, [runId]);

  const load = useCallback(() => {
    const mine = ++latest.current;
    const fetchPage = async () => {
      const options = { skips, ...(status ? { status } : {}) } as const;
      let next = await fetchScheduleRuns(scheduleId, { limit: Math.min(shown.current, RUNS_MAX), ...options });
      const cursors = new Set<string>();
      while (runId && !status && !next.runs.some((run) => run.run_id === runId)
        && next.next_before) {
        if (mine !== latest.current) return;
        if (cursors.has(next.next_before)) throw new Error("The run history did not advance");
        cursors.add(next.next_before);
        const older = await fetchScheduleRuns(scheduleId, {
          limit: RUNS_PAGE, before: next.next_before, ...options,
        });
        next = { ...older, runs: [...next.runs, ...older.runs] };
      }
      if (mine !== latest.current) return;
      shown.current = Math.max(RUNS_PAGE, next.runs.length);
      return next;
    };
    fetchPage().then((next) => {
        if (!next) return;
        if (mine !== latest.current) return;
        setPage(next);
        setError("");
      })
      .catch((err) => mine === latest.current && setError(errorText(err)));
  }, [scheduleId, skips, status, runId]);

  useEffect(() => { load(); return () => { latest.current++; }; }, [load, version, newest]);
  const active = !!page?.runs.some(isActiveRun);
  useEffect(() => {
    if (!open || !active) return;
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [open, active, load]);

  const showOlder = () => {
    if (!page?.next_before) return;
    const mine = ++latest.current;
    setLoadingMore(true);
    fetchScheduleRuns(scheduleId, { limit: RUNS_PAGE, before: page.next_before, skips,
      ...(status ? { status } : {}) })
      .then((next) => {
        if (mine !== latest.current) return;
        setPage((current) => current && { ...next, runs: [...current.runs, ...next.runs] });
        shown.current = page.runs.length + next.runs.length;
        setError("");
      })
      .catch((err) => mine === latest.current && setError(errorText(err)))
      .finally(() => setLoadingMore(false));
  };

  const missingRun = Boolean(page && runId && !status && !page.next_before
    && !page.runs.some((run) => run.run_id === runId));
  const skipCount = page ? page.routine_skips.skipped_gate + page.routine_skips.skipped_overlap : 0;
  return (
    <section aria-labelledby={`${id}-history`} className="space-y-2 border-t border-border pt-4">
      <div className={cn("flex flex-wrap items-center gap-x-4 gap-y-1", !isCommand && "justify-between")}>
        <h3 id={`${id}-history`} className="text-xs font-semibold">Run history</h3>
        {isCommand && <select aria-label="Run status" className={cn(SELECT, "w-auto")} value={status}
          onChange={(e) => { shown.current = RUNS_PAGE; setStatus(e.target.value as ScheduleRunStatus | ""); }}>
          <option value="">All</option>
          <option value="succeeded">Succeeded</option>
          <option value="failed">Failed</option>
          <option value="timed_out">Timed out</option>
          <option value="interrupted">Interrupted</option>
          <option value="skipped_overlap">Skipped (overlap)</option>
        </select>}
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-xs text-muted-foreground sm:min-h-0">
          <input
            type="checkbox"
            checked={hideSkips}
            disabled={Boolean(status)}
            onChange={(e) => {
              shown.current = RUNS_PAGE;
              setHideSkips(e.target.checked);
            }}
            className="h-4 w-4 accent-primary"
          />
          Hide routine skips (nothing new, busy)
          {skipCount > 0 && <span className="tabular-nums">· {skipCount.toLocaleString()} in all</span>}
        </label>
      </div>
      {isCommand && page?.stats && <p className="text-xs text-muted-foreground">
        {runStatsText(page.stats.day, "Last 24 h")} · {runStatsText(page.stats.week, "Last 7 days")}
      </p>}
      {missingRun && <p role="status" className="text-xs text-muted-foreground">This run is no longer kept</p>}
      {error && <p role="alert" className="text-xs text-destructive">Could not load the runs: {error}</p>}
      {!page ? (
        !error && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />
      ) : page.runs.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          {hideSkips && skipCount > 0 ? "No runs but routine skips" : "No runs yet"}
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {page.runs.map((run) => {
            if (isCommand) return <CommandRun key={run.run_id} run={run} timeZone={schedule.timezone} linked={run.run_id === runId} />;
            const text = run.error ?? run.summary;
            const took = durationText(run);
            return (
              <li key={run.run_id} className="flex flex-col gap-1 py-2 sm:flex-row sm:flex-wrap sm:items-center sm:gap-x-3">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs sm:shrink-0">
                  <time
                    dateTime={run.scheduled_for}
                    title={run.fired_at ? `Fired ${formatInZone(run.fired_at, schedule.timezone)}` : "Not fired yet"}
                    className="tabular-nums text-foreground"
                  >
                    {formatInZone(run.scheduled_for, schedule.timezone)}
                  </time>
                  <span className="text-muted-foreground">{triggerText(run)}</span>
                  <RunStatusBadge status={run.status} />
                  {run.firings > 1 && (
                    <span className="tabular-nums text-muted-foreground">
                      ×{run.firings}, until{" "}
                      <time dateTime={run.finished_at ?? undefined}>
                        {run.finished_at ? formatInZone(run.finished_at, schedule.timezone) : "now"}
                      </time>
                    </span>
                  )}
                  {took && <span className="tabular-nums text-muted-foreground" title="How long it took">{took}</span>}
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
                    className="inline-flex min-w-0 max-w-full shrink-0 items-center gap-1 self-start rounded-sm text-xs font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring sm:ml-auto sm:max-w-[40%] sm:self-auto"
                  >
                    <span className="truncate">{run.task_title || "Its task"}</span>
                    {run.task_status && <span className="shrink-0 font-normal text-muted-foreground">· {run.task_status}</span>}
                    <ArrowRight className="h-3 w-3 shrink-0" aria-hidden="true" />
                  </a>
                )}
                {run.carried_from && (
                  <p className="text-xs text-muted-foreground sm:basis-full">Carries the input of the previous run</p>
                )}
                <SubscriptionRun run={run} />
                {run.gate_output && (
                  <details className="min-w-0 text-xs sm:basis-full">
                    <summary className="w-fit cursor-pointer rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
                      Gate output
                    </summary>
                    <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/20 px-3 py-2 font-mono text-[11px] text-foreground/90">
                      {run.gate_output}
                    </pre>
                  </details>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {page?.next_before && (
        <Button type="button" variant="outline" size="sm" onClick={showOlder} disabled={loadingMore} className="h-11 sm:h-8">
          {loadingMore && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          Show older runs
        </Button>
      )}
    </section>
  );
}

// --- The tab ---

/** A schedule's note: what "Run now" answered, or why a change failed. */
export function NoticeText({ notice }: { notice: Notice }) {
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

/** The last run's result, the failures in a row and the active alerts, as a schedule's row shows them. */
export function LastRun({ schedule }: { schedule: ScheduleInfo }) {
  const failures = schedule.consecutive_failures;
  const { failures_since: failing, stale_since: stale } = schedule.alerts;
  const at = (iso: string) => formatInZone(iso, schedule.timezone);
  const lastSuccess = schedule.last_success_at ? at(schedule.last_success_at) : "never";
  const count = failures === 1 ? "1 failure" : `${failures} failures in a row`;
  return (
    <>
      <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
        {schedule.last_run ? (
          <>
            Last run
            <RunStatusBadge status={schedule.last_run.status} />
            {schedule.last_run.firings > 1 && <span className="tabular-nums">×{schedule.last_run.firings}</span>}
          </>
        ) : (
          "No runs yet"
        )}
      </span>
      {/* While the failures alert is on, its badge carries the count. */}
      {failing ? (
        <Badge
          variant="destructive"
          className="shrink-0 gap-1"
          title={failures > 0 ? `${count}; alert since ${at(failing)}` : `Alert since ${at(failing)}`}
        >
          <AlertCircle className="h-3 w-3" aria-hidden="true" />
          Failing{failures > 0 && ` · ${failures} in a row`}
        </Badge>
      ) : (
        failures > 0 && (
          <Badge variant={failures >= (schedule.alert_policy?.failures_after?.value ?? 3) ? "destructive" : "warning"} className="shrink-0 gap-1">
            <AlertCircle className="h-3 w-3" aria-hidden="true" />
            {count}
          </Badge>
        )
      )}
      {stale && (
        <Badge
          variant="destructive"
          className="shrink-0 gap-1"
          title={`No success for a while: alert since ${at(stale)}; last success ${lastSuccess}`}
        >
          <AlertCircle className="h-3 w-3" aria-hidden="true" />
          No success lately
        </Badge>
      )}
      {schedule.alerts.deadline_since && <span className="basis-full break-words text-xs text-destructive">
        Missed its success deadline since {at(schedule.alerts.deadline_since)}
      </span>}
      {schedule.alerts.overlap_since && <span className="basis-full break-words text-xs text-warning">
        Skipping: the previous run is still going since {at(schedule.alerts.overlap_since)}
      </span>}
    </>
  );
}

/**
 * A project's Schedules tab: one row per schedule (its rule, next run, last result, an on/off switch and "Run now"),
 * opening to every field and its run history, and a form for a new one. `linked`
 * (#projects/<id>/schedules/<schedule id>[/runs/<run id>]) opens a schedule and optionally one run;
 * `onLink` changes that address.
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
  // Only the schedule id is a collapsible row; a run suffix focuses its history below.
  const [scheduleLink, runLink] = linked.split("/runs/");
  const linkedSchedule = decodeURIComponent(scheduleLink);
  const linkedRun = runLink ? decodeURIComponent(runLink) : "";
  const row = useCollapsibleRows(linkedSchedule, data !== null, linkedRun);
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
          A schedule runs an agent task or shell command of this project, or reads a message stream, on a time rule. A new one
          starts disabled; turn it on when it is ready.
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
            const hold = schedule.usage_hold
              ? usageWindowWait(schedule.usage_hold.until, schedule.timezone)
              : null;
            const next = !schedule.enabled
              ? hold ? `Disabled · ${hold}` : "Disabled"
              : hold
              ? hold
              : schedule.next_fire_at
                ? `next ${formatInZone(schedule.next_fire_at, schedule.timezone)}`
                : "no next run";
            return (
              <CollapsibleRow
                key={sid}
                id={`schedule-${sid}`}
                {...state}
                name={schedule.name}
                marks={schedule.kind === "command" ? <Badge variant="outline">Command</Badge>
                  : schedule.kind === "subscription" && <Badge variant="outline">Subscription</Badge>}
                detail={schedule.description}
                summary={next}
                wrapActions={Boolean(schedule.alerts.deadline_since || schedule.alerts.overlap_since)}
                actions={
                  <>
                    {/* On a phone the summary is cut short: the next run again, for the eye only (the row's button
                        names it). */}
                    <span aria-hidden="true" className="basis-full truncate text-xs text-muted-foreground sm:hidden">
                      {next.charAt(0).toUpperCase() + next.slice(1)} · {schedule.description}
                    </span>
                    <LastRun schedule={schedule} />
                    <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
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
                      {schedule.kind === "agent" && <ResetPlannerSession schedule={schedule} onReset={put} />}
                    </div>
                    {schedule.kind === "agent" && <PlannerSessionStatus schedule={schedule} />}
                    {schedule.kind === "subscription" && <SubscriptionStatus schedule={schedule} onChange={put} />}
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
                        if (linkedSchedule === sid) onLink("");
                      }}
                    />
                    <RunHistory schedule={schedule} open={state.state === "open"} version={runsVersion[sid] ?? 0}
                      runId={linkedSchedule === sid ? linkedRun : ""} />
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
