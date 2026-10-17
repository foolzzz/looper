import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Loader2, Play } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { DeleteSchedule, LastRun, NoticeText, RunHistory, ScheduleTime } from "@/components/project-schedules";
import { fetchSchedule, runSchedule, updateSchedule, type ScheduleInfo, type TaskRole } from "@/lib/api";
import { roleLabel } from "@/lib/runtime-catalog";
import { NOTIFY_OPTIONS, TASK_LIMIT_ROLES, runNotice, useNotices } from "@/lib/schedules";
import { errorText } from "@/lib/use-action";
import { profileName, useRegistry } from "@/lib/use-registry";
import { formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";

/** How often the schedule (next run, last result, alerts, today's spend) refreshes. */
const REFRESH_MS = 10_000;
const ROLES: TaskRole[] = ["planner", "worker", "reviewer"];
const linkClass =
  "inline-flex items-center gap-1 rounded-sm text-primary hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";
const CODE = "break-all rounded bg-muted/30 px-1.5 py-0.5 font-mono text-xs text-foreground/90";

function seconds(value: number): string {
  if (value % 3600 === 0) return `${value / 3600} h`;
  if (value % 60 === 0) return `${value / 60} min`;
  return `${value} s`;
}

/** A field of the schedule: its name, then what it is set to. */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 space-y-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-sm">{children}</dd>
    </div>
  );
}

/**
 * One schedule (`#schedules/<id>`): its settings, its alerts and today's spend, its switch, "Run now" and "Delete
 * schedule", and the runs it fired with their tasks. It is edited on its project's Schedules tab, which "Edit" opens.
 * `onDeleted` runs once it is deleted (back to the list).
 */
export function ScheduleDetail({ scheduleId, onDeleted }: { scheduleId: string; onDeleted: () => void }) {
  const [schedule, setSchedule] = useState<ScheduleInfo | null>(null);
  const [error, setError] = useState("");
  /** The enabled value being saved. */
  const [switching, setSwitching] = useState<boolean | null>(null);
  const [starting, setStarting] = useState(false);
  /** Bumped to reload the run history after "Run now". */
  const [runsVersion, setRunsVersion] = useState(0);
  const { notices, notify } = useNotices();
  const { registry } = useRegistry();
  const latest = useRef(0);

  const load = useCallback(() => {
    // A slow poll must not overwrite a newer answer, nor one this page just wrote.
    const mine = ++latest.current;
    fetchSchedule(scheduleId)
      .then((next) => {
        if (mine !== latest.current) return;
        setSchedule(next);
        setError("");
      })
      .catch((err) => mine === latest.current && setError(errorText(err)));
  }, [scheduleId]);

  useEffect(() => {
    load();
    const timer = setInterval(load, REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const put = (next: ScheduleInfo) => {
    latest.current++;
    setSchedule(next);
  };

  const toggle = async (enabled: boolean) => {
    setSwitching(enabled);
    notify(scheduleId, null);
    try {
      put(await updateSchedule(scheduleId, { enabled }));
      notify(scheduleId, { text: enabled ? "Turned on" : "Turned off", tone: "ok" });
    } catch (err) {
      notify(scheduleId, { text: `Could not turn it ${enabled ? "on" : "off"}: ${errorText(err)}`, tone: "error" });
    } finally {
      setSwitching(null);
    }
  };

  const runNow = async () => {
    setStarting(true);
    notify(scheduleId, null);
    try {
      notify(scheduleId, runNotice(await runSchedule(scheduleId)));
      setRunsVersion((v) => v + 1);
      fetchSchedule(scheduleId).then(put, () => {});
    } catch (err) {
      notify(scheduleId, { text: errorText(err), tone: "error" });
    } finally {
      setStarting(false);
    }
  };

  const back = (
    <a href="#schedules" className={cn("text-sm", linkClass)}>
      <ArrowLeft className="h-3.5 w-3.5" aria-hidden="true" />
      All schedules
    </a>
  );

  if (!schedule)
    return (
      <div className="space-y-4 p-6">
        {back}
        {error ? (
          <div className="flex flex-wrap items-center gap-3">
            <p role="alert" className="text-sm text-destructive">Could not load the schedule: {error}</p>
            <Button type="button" variant="outline" size="sm" onClick={load}>
              Retry
            </Button>
          </div>
        ) : (
          <div className="flex h-24 items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" aria-label="Loading" />
          </div>
        )}
      </div>
    );

  const enabled = switching ?? schedule.enabled;
  const editHref = `#projects/${encodeURIComponent(schedule.project_id)}/schedules/${encodeURIComponent(schedule.schedule_id)}`;
  const profiles = ROLES.filter((role) => schedule.profile_overrides[role]);
  const limited = TASK_LIMIT_ROLES.some((role) => schedule.task_limits[role] != null);
  const notifyOption = NOTIFY_OPTIONS.find((option) => option.value === schedule.notify);
  const profileLabel = (profileId: string) => {
    const profile = registry?.profiles.find((p) => p.profile_id === profileId);
    return profile ? profileName(profile) : profileId;
  };

  return (
    <div className="space-y-6 p-6">
      {back}
      <header className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 flex-1 basis-72 space-y-1">
          <h1 className="flex min-w-0 flex-wrap items-center gap-2 text-2xl font-bold">
            <span className="min-w-0 break-words">{schedule.name}</span>
            {!schedule.enabled && <Badge variant="outline">Off</Badge>}
          </h1>
          <p className="text-sm text-muted-foreground">
            In{" "}
            <a href={`#projects/${encodeURIComponent(schedule.project_id)}`} className={linkClass}>
              {schedule.project_name}
            </a>
            {" · "}
            {schedule.description}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex h-11 cursor-pointer items-center gap-2 text-xs text-muted-foreground sm:h-9">
            <Switch checked={enabled} onChange={toggle} disabled={switching !== null} label={`Enable ${schedule.name}`} />
            <span className="w-6" aria-hidden="true">{enabled ? "On" : "Off"}</span>
          </label>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={runNow}
            disabled={starting}
            aria-label={`Run now: ${schedule.name}`}
            className="h-11 sm:h-8"
          >
            {starting ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Play className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            Run now
          </Button>
          <a href={editHref} className={cn("min-h-11 px-1 text-sm sm:min-h-0", linkClass)}>
            Edit on the project
            <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
          <DeleteSchedule schedule={schedule} onDeleted={onDeleted} />
        </div>
        {notices[scheduleId] && <NoticeText notice={notices[scheduleId]} />}
      </header>

      {error && <p role="alert" className="text-xs text-destructive">Could not refresh the schedule: {error}</p>}

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <span className="text-muted-foreground">
          {schedule.enabled ? (
            schedule.next_fire_at ? (
              <>
                Next{" "}
                <ScheduleTime iso={schedule.next_fire_at} timeZone={schedule.timezone} className="tabular-nums text-foreground" />
              </>
            ) : (
              "No next run"
            )
          ) : (
            "Disabled: it does not run on its rule"
          )}
        </span>
        <LastRun schedule={schedule} />
        <span className="tabular-nums text-xs text-muted-foreground">
          Today {formatUsd(schedule.spent_today_usd)}
          {schedule.daily_budget_usd != null && ` of ${formatUsd(schedule.daily_budget_usd)} a day`}
        </span>
      </div>

      <section aria-labelledby="schedule-settings" className="rounded-lg border border-border bg-card px-5 py-4">
        <h2 id="schedule-settings" className="mb-3 text-sm font-semibold">Settings</h2>
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="Rule">
            {schedule.description} <code className={CODE}>{schedule.cron}</code>
          </Field>
          <Field label="Time zone">{schedule.timezone}</Field>
          <Field label="Work directory">
            {schedule.work_dir ? <code className={CODE}>{schedule.work_dir}</code> : "The project's default"}
          </Field>
          <Field label="Gate">
            {schedule.gate_command ? (
              <>
                <code className={CODE}>{schedule.gate_command}</code>
                {schedule.gate_timeout_seconds != null && (
                  <span className="text-muted-foreground"> · at most {seconds(schedule.gate_timeout_seconds)}</span>
                )}
              </>
            ) : (
              "None: every run starts a task"
            )}
          </Field>
          <Field label="Notify">
            {notifyOption?.label ?? schedule.notify}
            {schedule.notify !== "never" &&
              (schedule.notify_target
                ? ` → ${schedule.notify_target.channel === "telegram" ? "Telegram" : "Lark"} · ${schedule.notify_target.chat}`
                : " → the dashboard only")}
            {notifyOption && <p className="text-xs text-muted-foreground">{notifyOption.hint}</p>}
          </Field>
          <Field label="Budgets">
            <span className="block">
              Per run: {schedule.run_budget_usd != null ? formatUsd(schedule.run_budget_usd) : "the project's task cap"}
            </span>
            <span className="block">
              Per day: {schedule.daily_budget_usd != null ? formatUsd(schedule.daily_budget_usd) : "no cap"}
            </span>
          </Field>
          <Field label="Run timeout">
            {schedule.run_timeout_seconds != null ? seconds(schedule.run_timeout_seconds) : "None"}
          </Field>
          <Field label="When a run is still going">
            {schedule.overlap === "queue"
              ? "The next one waits (at most one)"
              : schedule.overlap === "parallel"
                ? `The next one starts beside it, up to ${schedule.max_parallel_runs} at once (reentrant)`
                : "The next one is skipped"}
            {" · after downtime: "}
            {schedule.catch_up === "once" ? "run once" : "skip what was missed"}
          </Field>
          <Field label="Agent Profiles">
            {profiles.length === 0
              ? "As the project sets them"
              : profiles.map((role) => (
                  <span key={role} className="block">
                    {roleLabel(role)}: {profileLabel(schedule.profile_overrides[role] as string)}
                  </span>
                ))}
          </Field>
          {limited && (
            <Field label="At once in each run's task">
              {TASK_LIMIT_ROLES.map((role) => (
                <span key={role} className="block">
                  {roleLabel(role)}s: {schedule.task_limits[role] ?? "the default (Settings → Roles)"}
                </span>
              ))}
            </Field>
          )}
        </dl>
        <details className="mt-4 text-sm">
          <summary className="w-fit cursor-pointer rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
            Requirement
          </summary>
          <p className="mt-2 whitespace-pre-wrap break-words rounded-md border border-border bg-muted/20 px-3 py-2 text-sm text-foreground/90">
            {schedule.requirement}
          </p>
        </details>
      </section>

      <RunHistory schedule={schedule} open version={runsVersion} />
    </div>
  );
}
