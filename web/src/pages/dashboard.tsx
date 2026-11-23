import { startPolling } from "@/lib/polling";
import { useWaitingGates } from "@/lib/use-waiting-gates";
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { DashboardGates } from "@/components/dashboard-gates";
import { StatusCard } from "@/components/status-card";
import { ManagerSessions } from "@/components/manager-sessions";
import { TaskQualitySection } from "@/components/task-quality-section";
import { RunStatusBadge, ScheduleTime } from "@/components/project-schedules";
import {
  fetchOverview,
  TASK_GROUPS,
  type AgentProcess,
  type TokenRate,
  type ComponentHealth,
  type Overview,
  type OverviewSchedules,
  type TaskGroup,
} from "@/lib/api";
import { fetchManagerMetrics, type ManagerMetrics as Metrics } from "@/lib/manager-metrics";
import { historyHref } from "@/pages/agent-history";
import { formatTpm, formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";

/** The groups always counted; "Needs you", "Cancelled" and "Other" show only when they hold a task. */
const ALWAYS_SHOWN: TaskGroup[] = ["in_progress", "ready", "attention", "completed"];

const linkClass =
  "rounded-sm hover:text-primary hover:underline underline-offset-2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** 45 s, 12 min, 2 h 5 min. */
function elapsed(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const rest = minutes % 60;
  return `${Math.floor(minutes / 60)} h${rest ? ` ${rest} min` : ""}`;
}

function Section({ title, meta, children }: { title: string; meta?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="rounded-lg border border-border bg-card px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold">{title}</h2>
        {meta && <span className="text-xs text-muted-foreground">{meta}</span>}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}

const clockTime = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

/** What the supervisor is doing about a component that is not running. */
function healthText(c: ComponentHealth): string {
  const inARow =
    c.restart_count === 0 ? "" : c.restart_count === 1 ? "Restarted once. " : `Restarted ${c.restart_count} times in a row. `;
  if (c.state === "crash_looping" && c.up)
    return `${inARow}It is up now and counts as stable after 5 minutes without a crash.`;
  const next = c.next_restart_at ? `It restarts at ${clockTime(c.next_restart_at)}` : "It restarts at the next health check";
  return `${inARow}${next}; each wait doubles, up to 5 minutes, and it is never given up on.`;
}

function Health({ unhealthy }: { unhealthy: Overview["health"]["unhealthy"] }) {
  if (unhealthy.length === 0) {
    return (
      <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <CheckCircle2 className="h-3.5 w-3.5 text-success" aria-hidden="true" />
        All components healthy
      </p>
    );
  }
  return (
    <div role="alert" className="space-y-1 rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3 text-sm">
      {unhealthy.map((c) => (
        <p key={c.name} className="flex gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
          <span>
            <span className="font-medium text-destructive">
              {c.state === "crash_looping" ? `${c.name} keeps crashing.` : `${c.name} is down.`}
            </span>{" "}
            {healthText(c)}
          </span>
        </p>
      ))}
    </div>
  );
}

function TaskCounts({ tasks }: { tasks: Overview["tasks"] }) {
  const shown = TASK_GROUPS.filter((g) => ALWAYS_SHOWN.includes(g.id) || tasks[g.id] > 0);
  const others = Object.entries(tasks.other_statuses).map(([status, n]) => `${status} ${n}`).join(", ");
  return (
    <nav aria-label="Tasks by group" className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {shown.map((g) => {
        const count = tasks[g.id];
        const tone =
          count === 0
            ? ""
            : g.id === "in_progress"
              ? "text-primary"
              : g.id === "attention" || g.id === "needs_you"
                ? "text-warning"
                : "";
        return (
          <a
            key={g.id}
            href={`#tasks?group=${g.id}&scheduled=include`}
            title={g.id === "other" && others ? `Statuses no group knows: ${others}` : undefined}
            className="rounded-lg border border-border bg-card px-4 py-3 transition-colors hover:border-primary/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <p className="text-xs text-muted-foreground">{g.label}</p>
            <p className={cn("mt-1 text-2xl font-bold tabular-nums", tone)}>{count}</p>
          </a>
        );
      })}
    </nav>
  );
}

function AgentTask({ taskId, name, project }: { taskId: string; name: string; project: string | null }) {
  const label = project ? `${project} · ${name}` : name;
  return (
    <a href={`#tasks/${encodeURIComponent(taskId)}`} title={label} className={cn("inline-block max-w-full text-sm", linkClass)}>
      <span className="flex min-w-0 items-baseline gap-1">
        {project && <><span title={project} className="max-w-[40%] shrink-0 truncate text-muted-foreground">{project}</span>{" · "}</>}
        <span title={name} className="min-w-0 truncate">{name}</span>
      </span>
    </a>
  );
}

const ROLE_LABEL: Record<AgentProcess["role"], string> = {
  manager: "Manager", planner: "Planner", worker: "Worker", reviewer: "Reviewer", consult: "Consult",
  processor: "Processor",
};

/** A figure whose breakdown is its hover text, reachable by keyboard. */
const focusable = "rounded-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

/** The last minute's tokens by kind, as a hover text. */
function tpmBreakdown(t: TokenRate, windowSeconds = 60): string {
  const n = (v: number) => v.toLocaleString("en-US");
  return `Input ${n(t.input)} · Output ${n(t.output)} · Cache read ${n(t.cache_read)} · Cache write ${n(t.cache_creation)} · last ${windowSeconds} s`;
}

function Tpm({ rate, windowSeconds, running = true }: { rate: TokenRate | null; windowSeconds?: number; running?: boolean }) {
  if (!rate) {
    return <span title={running ? "Usage is reported when a turn ends" : undefined} className="text-muted-foreground">—</span>;
  }
  const text = tpmBreakdown(rate, windowSeconds);
  return <span tabIndex={0} title={text} aria-label={`${formatTpm(rate.total)} tokens per minute. ${text}`} className={focusable}>{formatTpm(rate.total)}</span>;
}

function ProcessRow({ p, windowSeconds }: { p: AgentProcess; windowSeconds: number }) {
  const instruction = (p.instruction ?? "").split("\n")[0] || p.step || "";
  const runtime = [p.runtime, p.model].filter(Boolean).join(" · ");
  const label = p.name && p.name !== ROLE_LABEL[p.role] ? `${ROLE_LABEL[p.role]} · ${p.name}` : ROLE_LABEL[p.role];
  return (
    <div role="row" className="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-3 gap-y-0.5 py-2 text-sm lg:grid-cols-[9rem_minmax(0,1fr)_14rem_4.5rem_4.5rem_4.5rem]">
      <div role="cell" className="col-start-1 row-start-1 flex min-w-0 items-baseline gap-2 lg:order-1 lg:col-auto lg:row-auto">
        <a href={historyHref(p.agent_id, p.task_id ?? undefined)} title={label} className={cn("min-w-0 truncate font-medium", linkClass)}>{label}</a>
        {p.task_role && <span title={p.task_role.id} className="max-w-20 shrink-0 truncate font-mono text-xs">{p.task_role.id}</span>}
      </div>
      <div role="cell" className="col-span-2 row-start-2 min-w-0 lg:order-2 lg:col-auto lg:row-auto">
        {p.task_id && <AgentTask taskId={p.task_id} name={p.task_name || p.task_id} project={p.project_name} />}
        {instruction && (p.role === "worker" || p.role === "reviewer") && (
          <p title={instruction} className="truncate text-xs text-muted-foreground">{instruction}</p>
        )}
      </div>
      <div className="col-span-2 row-start-3 flex flex-wrap gap-x-3 text-xs text-muted-foreground lg:contents">
        <span role="cell" title={runtime || undefined} className="min-w-0 truncate lg:order-3 lg:text-sm">{runtime || "—"}</span>
        <span role="cell" className="font-mono tabular-nums lg:order-4 lg:text-sm">
          <span className="lg:hidden">PID </span>{p.pid ?? "—"}
        </span>
        <span role="cell" className="tabular-nums lg:order-6 lg:text-right lg:text-sm lg:text-foreground">
          <span className="lg:hidden">TPM </span><Tpm rate={p.tpm} windowSeconds={windowSeconds} running={p.state === "running"} />
        </span>
      </div>
      <div role="cell" className="col-start-2 row-start-1 justify-self-end tabular-nums text-muted-foreground lg:order-5 lg:col-auto lg:row-auto lg:justify-self-start">
        {p.state === "waiting" ? <Badge variant="warning">waiting</Badge> : elapsed(p.elapsed_seconds)}
      </div>
    </div>
  );
}

const COLUMNS = ["Agent", "Task / step", "Runtime · model", "PID", "Time", "TPM"];

function Agents({ overview }: { overview: Overview }) {
  const { agents } = overview;
  const processes = overview.processes ?? [];
  const tpm = overview.tpm;
  const running = processes.filter((p) => p.state === "running").length;
  const waiting = processes.length - running;
  // A Manager without a row is named below: busy before its turn's run starts (its runtime starting, a session
  // switch) or idle between turns; a stopped one (or one suspended while idle) is left out, as before.
  const listed = new Set(processes.map((p) => p.agent_id));
  const resting = (agents.managers ?? (agents.manager ? [{ ...agents.manager, name: "Manager" }] : []))
    .filter((m) => !listed.has(m.agent_id));
  const restLine = (["busy", "idle"] as const).map((state) => {
    const names = resting.filter((m) => m.state === state).map((m) => m.name);
    return names.length ? `${state === "busy" ? "Busy" : "Idle"}: ${names.join(", ")}` : "";
  }).filter(Boolean).join(" · ");
  return (
    <Section
      title="Agents running now"
      meta={<>
        {running} running{waiting > 0 && ` · ${waiting} waiting`}
        {tpm && <> · <span tabIndex={0} title={tpmBreakdown(tpm, tpm.window_seconds)} className={cn("tabular-nums", focusable)}>{formatTpm(tpm.total)} TPM</span></>}
      </>}
    >
      {processes.length === 0 ? (
        <p className="text-sm text-muted-foreground">No agent is running now</p>
      ) : (
        <div role="table" aria-label="Agent processes">
          <div role="row" className="hidden gap-x-3 border-b border-border pb-1.5 text-xs text-muted-foreground lg:grid lg:grid-cols-[9rem_minmax(0,1fr)_14rem_4.5rem_4.5rem_4.5rem]">
            {COLUMNS.map((c, i) => <span key={c} role="columnheader" className={i === 5 ? "text-right" : undefined}>{c}</span>)}
          </div>
          <div role="rowgroup" className="divide-y divide-border">
            {processes.map((p, i) => <ProcessRow key={`${p.agent_id}-${i}`} p={p} windowSeconds={tpm?.window_seconds ?? 60} />)}
          </div>
        </div>
      )}
      {restLine && <p className="mt-3 text-xs text-muted-foreground">{restLine}</p>}
    </Section>
  );
}

function Load({
  label,
  running,
  waiting = 0,
  extra,
}: {
  label: string;
  running: number;
  waiting?: number;
  extra?: string;
}) {
  return (
    <span className="whitespace-nowrap tabular-nums">
      <span className="text-muted-foreground">{label} </span>
      {running}
      {waiting > 0 && <span className="text-muted-foreground"> · {waiting} waiting</span>}
      {extra && <span className="text-muted-foreground"> · {extra}</span>}
    </span>
  );
}

function Concurrency({ projects, idle }: { projects: Overview["projects"]; idle: number }) {
  return (
    <Section title="Running by project" meta="running now">
      {projects.length === 0 ? (
        <p className="text-sm text-muted-foreground">No project has a task in progress or ready.</p>
      ) : (
        <ul className="divide-y divide-border">
          {projects.map((p) => (
            <li key={p.project_id} className="flex flex-wrap items-baseline gap-x-5 gap-y-1 py-2 text-sm first:pt-0 last:pb-0">
              <a href={`#projects/${encodeURIComponent(p.project_id)}`} className={cn("min-w-0 max-w-full truncate font-medium sm:w-40", linkClass)}>
                {p.name}
              </a>
              <Load label="tasks" running={p.tasks.running} extra={p.tasks.ready ? `${p.tasks.ready} ready` : undefined} />
              <Load label="workers" running={p.workers.running} waiting={p.workers.waiting} />
              <Load label="reviewers" running={p.reviewers.running} waiting={p.reviewers.waiting} />
            </li>
          ))}
        </ul>
      )}
      {idle > 0 && (
        <p className="mt-2 text-xs text-muted-foreground">
          +{idle} idle {idle === 1 ? "project" : "projects"}
        </p>
      )}
    </Section>
  );
}

/** Today's skipped runs by reason, in words. */
const SKIPPED: { key: keyof OverviewSchedules["skipped_today"]; label: string }[] = [
  { key: "skipped_gate", label: "nothing new" },
  { key: "skipped_overlap", label: "previous run still going" },
  { key: "skipped_missed", label: "missed" },
  { key: "skipped_budget", label: "daily budget" },
  { key: "skipped_busy", label: "Manager busy" },
];

const subhead = "text-xs font-medium text-muted-foreground";

const scheduleHref = (s: { project_id: string; schedule_id: string }) =>
  `#projects/${encodeURIComponent(s.project_id)}/schedules/${encodeURIComponent(s.schedule_id)}`;

function Schedules({ schedules }: { schedules: OverviewSchedules }) {
  const { total, enabled, next, failing, skipped_today } = schedules;
  const skipped = SKIPPED.filter((reason) => skipped_today[reason.key] > 0);
  return (
    <Section title="Schedules" meta={`${enabled} of ${total} enabled`}>
      <div className="grid gap-x-6 gap-y-4 lg:grid-cols-3">
        <div className="min-w-0">
          <h3 className={subhead}>Next</h3>
          {next.length === 0 ? (
            <p className="mt-1.5 text-sm text-muted-foreground">No schedule is enabled</p>
          ) : (
            <ul className="mt-1.5 space-y-2">
              {next.map((s) => (
                <li key={s.schedule_id} className="min-w-0 text-sm">
                  <a href={scheduleHref(s)} className={cn("block truncate font-medium", linkClass)}>
                    {s.name}
                  </a>
                  <p className="truncate text-xs text-muted-foreground">
                    <ScheduleTime iso={s.next_fire_at} timeZone={s.timezone} className="tabular-nums text-foreground/90" />
                    {" · "}
                    {s.project_name}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="min-w-0">
          <h3 className={subhead}>Failing</h3>
          {failing.length === 0 ? (
            <p className="mt-1.5 text-sm text-muted-foreground">No failing schedules</p>
          ) : (
            <ul className="mt-1.5 space-y-2">
              {failing.map((s) => {
                const failures = s.consecutive_failures;
                return (
                  <li key={s.schedule_id} className="min-w-0 space-y-1 text-sm">
                    <a href={scheduleHref(s)} className={cn("block truncate font-medium", linkClass)}>
                      {s.name}
                    </a>
                    <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                      <span className="min-w-0 max-w-full truncate">{s.project_name}</span>
                      {/* An active alert is red; failures below its threshold are a warning. */}
                      {(failures > 0 || s.failures_since) && (
                        <Badge variant={s.failures_since ? "destructive" : "warning"} className="gap-1">
                          <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                          {failures === 0 ? "Failing" : failures === 1 ? "1 failure" : `${failures} failures in a row`}
                        </Badge>
                      )}
                      {s.stale_since && (
                        <Badge variant="destructive" className="gap-1">
                          <AlertTriangle className="h-3 w-3" aria-hidden="true" />
                          <span>
                            {s.last_success_at ? (
                              <>
                                No success since <ScheduleTime iso={s.last_success_at} timeZone={s.timezone} />
                              </>
                            ) : (
                              "No success yet"
                            )}
                          </span>
                        </Badge>
                      )}
                      {s.deadline_since && (
                        <span className="text-destructive">
                          Missed its success deadline since <ScheduleTime iso={s.deadline_since} timeZone={s.timezone} />
                        </span>
                      )}
                      {s.overlap_since && (
                        <span className="text-warning">
                          Skipping: the previous run is still going since <ScheduleTime iso={s.overlap_since} timeZone={s.timezone} />
                        </span>
                      )}
                      {s.last_status && (
                        <span className="flex items-center gap-1.5">
                          last run
                          <RunStatusBadge status={s.last_status} />
                        </span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <div className="min-w-0">
          <h3 className={subhead}>Skipped today</h3>
          {skipped.length === 0 ? (
            <p className="mt-1.5 text-sm text-muted-foreground">No skipped runs today</p>
          ) : (
            <ul className="mt-1.5 space-y-1 text-sm">
              {skipped.map((reason) => (
                <li key={reason.key} className="flex items-baseline gap-2">
                  <span className="min-w-6 text-right font-semibold tabular-nums">{skipped_today[reason.key]}</span>
                  <span className="text-muted-foreground">{reason.label}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Section>
  );
}

function Spend({ spend }: { spend: Overview["spend_today"] }) {
  return (
    <Section title="Today's spend" meta={<span title={spend.since}>runs started since midnight, server time</span>}>
      <p className="flex flex-wrap items-baseline gap-x-4 gap-y-1 text-sm">
        <span>
          <span className="text-lg font-semibold tabular-nums">{formatUsd(spend.provider_priced_usd)}</span>{" "}
          <span className="text-muted-foreground">provider-priced</span>
        </span>
        <span>
          <span className="text-lg font-semibold tabular-nums">{formatUsd(spend.equivalent_usd)}</span>{" "}
          <span className="text-muted-foreground">subscription equivalent</span>
        </span>
      </p>
      <p className="mt-2 text-xs text-muted-foreground">
        Provider-priced is money: API-key runs at their provider&apos;s prices. The subscription equivalent is what a
        runtime&apos;s own login used, at API prices; it is not billed.
      </p>
      {spend.unpriced_runs > 0 && (
        <p className="mt-1 text-xs text-warning">
          Unpriced runs: {spend.unpriced_runs} (ended without a cost: no result, or no price for their model)
        </p>
      )}
    </Section>
  );
}

/** The Manager group's windows, in days; the choice is remembered per browser. */
const METRIC_WINDOWS = [{ days: 1, label: "24 h" }, { days: 7, label: "7 d" }] as const;
const METRIC_WINDOW_KEY = "looper.dashboard.managerMetricsDays";
const METRICS_REFRESH_MS = 60_000;

function savedWindow(): number {
  try {
    return window.localStorage.getItem(METRIC_WINDOW_KEY) === "1" ? 1 : 7;
  } catch {
    return 7;
  }
}

/** What each proactive source is, in words; an unknown one shows its key. */
const PROACTIVE_SOURCES: Record<string, string> = {
  task_notice: "task notices", completion: "completion turns", progress: "progress", gate: "gates",
  gate_reminder: "gate reminders", subscription: "subscriptions", intent: "intents", schedule: "schedules",
  advisor_alert: "advisor alerts", autonomous: "autonomous turns", user_reminder: "reminders",
  idle_check: "idle checks", triage: "event triage",
};
const sourceLabel = (source: string) => PROACTIVE_SOURCES[source] ?? source;

/** Milliseconds as seconds with one decimal; a dash when unknown, never 0. */
const seconds = (ms: number | null) => (ms === null ? "—" : `${(ms / 1000).toFixed(1)} s`);
/** A ratio as a whole percent; a dash when unknown. */
const percent = (ratio: number | null | undefined) => (ratio == null ? "—" : `${Math.round(ratio * 100)}%`);

function Stat({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-lg border border-border px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-xl font-bold tabular-nums">{value}</p>
      {detail && <p className="mt-0.5 text-xs text-muted-foreground">{detail}</p>}
    </div>
  );
}


/** A date as YYYY-MM-DD in the browser's zone (the server's, on the machine that serves the dashboard). */
function localDate(when: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}`;
}

/** Every local day the window touches, newest first: a window of N days spans N + 1 dates (today and N back), and
 * a day the answer names that falls outside them is kept too, so nothing it counted is dropped. */
function windowDays(windowDays: number, named: string[], now = new Date()): string[] {
  const days = new Set(named);
  for (let back = 0; back <= windowDays; back++) {
    days.add(localDate(new Date(now.getFullYear(), now.getMonth(), now.getDate() - back)));
  }
  return [...days].sort().reverse();
}

function MetricFigures({ metrics }: { metrics: Metrics }) {
  const turns = Object.values(metrics.turns.by_delivery).reduce((sum, n) => sum + n, 0);
  const days = metrics.proactive.per_day;
  const sent = days.reduce((sum, day) => sum + Object.values(day.by_source).reduce((a, n) => a + n, 0), 0);
  const perDay = sent / metrics.window_days;
  const bySource = new Map(days.map((day) => [day.date, day.by_source]));
  const rows = windowDays(metrics.window_days, days.map((day) => day.date))
    .map((date) => ({ date, by_source: bySource.get(date) ?? {} }));
  const rates = Object.entries(metrics.proactive.reply_rate.by_source);
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Ack (P50)" value={seconds(metrics.ack.p50_ms)} detail={`P95 ${seconds(metrics.ack.p95_ms)}`} />
        <Stat label="First reply (P50)" value={seconds(metrics.first_reply.p50_ms)}
          detail={`P95 ${seconds(metrics.first_reply.p95_ms)}`} />
        <Stat label="Empty deliveries" value={metrics.empty.ratio === null ? "—" : `${metrics.empty.count} · ${percent(metrics.empty.ratio)}`}
          detail={turns > 0 ? `turns that delivered no reply, of ${turns}` : "no turn measured"} />
        <Stat label="Proactive messages" value={`${perDay.toFixed(1)} / day`}
          detail={`reply rate ${percent(metrics.proactive.reply_rate.all)}`} />
      </div>
      <OutboundFigures metrics={metrics} />
      <ActionFigures metrics={metrics} />
      <table className="w-full text-sm">
        <caption className="sr-only">Proactive messages by day</caption>
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            <th scope="col" className="py-1.5 pr-3 font-normal">Day</th>
            <th scope="col" className="py-1.5 pr-3 font-normal">By source</th>
            <th scope="col" className="py-1.5 text-right font-normal">Messages</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((day) => (
            <tr key={day.date}>
              <td className="whitespace-nowrap py-1.5 pr-3 tabular-nums">{day.date}</td>
              <td className="py-1.5 pr-3 text-muted-foreground">
                {Object.entries(day.by_source).map(([source, n]) => `${sourceLabel(source)} ${n}`).join(" · ") || "—"}
              </td>
              <td className="py-1.5 text-right tabular-nums">
                {Object.values(day.by_source).reduce((a, n) => a + n, 0)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rates.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Replied within 30 minutes: {rates.map(([source, rate]) => `${sourceLabel(source)} ${percent(rate)}`).join(" · ")}
        </p>
      )}
    </div>
  );
}

const ACTION_CATEGORIES: Record<string, string> = {
  create: "created", update: "updated", control: "controlled", remove: "removed", relay: "relayed",
};

/** What the Manager and API clients wrote over the window, by category; failed writes in brackets. Absent while
 * action receipts are off (no row has a category then). */
function ActionFigures({ metrics }: { metrics: Metrics }) {
  const rows = Object.entries(metrics.actions?.by_category ?? {});
  if (rows.length === 0) return null;
  const phrase = ([category, byStatus]: [string, Record<string, number>]) => {
    const failed = byStatus.error ?? 0;
    return `${ACTION_CATEGORIES[category] ?? category} ${byStatus.ok ?? 0}${failed > 0 ? ` (${failed} failed)` : ""}`;
  };
  return (
    <p aria-label="Writes" className="text-xs text-muted-foreground">
      Writes: {rows.map(phrase).join(" · ")}
    </p>
  );
}

function OutboundFigures({ metrics }: { metrics: Metrics }) {
  const channels = metrics.outbound;
  if (!channels) return null;
  if (!Array.isArray(channels)) {
    return (
      <p aria-label="出站" className="text-sm text-muted-foreground">
        出站网关未启用。在{" "}
        <a href="#settings/chat" className={cn("text-primary", linkClass)}>Settings → Chat</a> 中启用。
      </p>
    );
  }
  return (
    <div aria-label="出站" className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 className="text-xs font-medium text-muted-foreground">出站</h3>
        <p className="text-xs text-muted-foreground">近 24 小时 · 排队为当前值</p>
      </div>
      {metrics.outbound_since_start && metrics.outbound_started_at && (
        <p className="text-xs text-muted-foreground">
          退避、熔断：自 <time dateTime={metrics.outbound_started_at}>
            {new Date(metrics.outbound_started_at).toLocaleString("zh-CN", { hour12: false })}
          </time> 起（服务启动后）
        </p>
      )}
      {channels.length === 0 ? (
        <p className="text-sm text-muted-foreground">暂无出站渠道</p>
      ) : (
        <ul className="space-y-2">
          {channels.map((channel) => {
            const failed = Object.values(channel.failed_24h).reduce((sum, count) => sum + count, 0);
            const attention = channel.breaker_state === "open" || failed > 0;
            const state = channel.breaker_state === null ? "未运行" : {
              closed: "未熔断", open: "已熔断", half_open: "探测中",
            }[channel.breaker_state];
            return (
              <li key={channel.channel_id} className={cn(
                "flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border px-3 py-2 text-sm",
                attention ? "border-warning/50" : "border-border",
              )}>
                <span className={cn("min-w-0 break-words font-medium", attention && "text-warning")}>
                  {channel.name}
                </span>
                <span className="tabular-nums">排队 {channel.queued}</span>
                <span className="tabular-nums">退避 {channel.backoffs_24h}</span>
                <span className="inline-flex items-center gap-1.5 tabular-nums">
                  熔断 {channel.breaker_opens_24h}
                  <Badge variant={channel.breaker_state === "open" ? "warning" : "outline"}>{state}</Badge>
                </span>
                <span className={cn("tabular-nums", failed > 0 && "text-warning")}>失败 {failed}</span>
                <span className="tabular-nums">不确定 {channel.uncertain_24h}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** The Manager's turns and proactive messages (`manager_metrics.get`), read on its own: a failed read shows here
 * and leaves the rest of the page alone. */
function ManagerMetricsSection() {
  const [days, setDays] = useState(savedWindow);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    return startPolling(async (signal) => {
      await fetchManagerMetrics(days, signal)
        .then((m) => { if (!signal.aborted) { setMetrics(m); setError(""); } })
        .catch((e) => { if (!signal.aborted) setError(e instanceof Error ? e.message : String(e)); });
    }, METRICS_REFRESH_MS);
  }, [days]);

  const choose = (next: number) => {
    if (next === days) return;
    setDays(next);
    setMetrics(null);  // the other window's figures are not this one's
    setError("");
    try {
      window.localStorage.setItem(METRIC_WINDOW_KEY, String(next));
    } catch {
      // A browser that keeps nothing keeps the default.
    }
  };

  return (
    <Section
      title="Manager"
      meta={metrics?.enabled === false ? undefined : (
        <span role="group" aria-label="Metrics window" className="inline-flex gap-1">
          {METRIC_WINDOWS.map((w) => (
            <button key={w.days} type="button" aria-pressed={days === w.days} onClick={() => choose(w.days)}
              className={cn("rounded-md px-2 py-0.5 pointer-coarse:min-h-11 pointer-coarse:px-3", focusable,
                days === w.days ? "bg-primary/10 text-primary" : "hover:text-foreground")}>
              {w.label}
            </button>
          ))}
        </span>
      )}
    >
      {error && (
        <p role="alert" className={cn("text-xs text-destructive", metrics && "mb-3")}>
          Cannot load the Manager&apos;s metrics ({error}){metrics ? "; these are the last numbers read." : "."}
        </p>
      )}
      {error && !metrics ? null : !metrics ? (
        <p className="text-sm text-muted-foreground">Loading…</p>
      ) : !metrics.enabled ? (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Metrics are off. Turn on “记录管家体验指标” in{" "}
            <a href="#settings/chat" className={cn("text-primary", linkClass)}>Settings → Chat</a> to start recording.
          </p>
          <OutboundFigures metrics={metrics} />
        </div>
      ) : (
        <MetricFigures metrics={metrics} />
      )}
      <ManagerSessions />
    </Section>
  );
}

export function DashboardPage() {
  const waitingGates = useWaitingGates();
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    return startPolling(async (signal) => {
      await fetchOverview()
        .then((o) => {
          if (signal.aborted) return;
          setOverview(o);
          setError("");
        })
        .catch((e) => { if (!signal.aborted) setError(e.message); });
    }, 5000);
  }, []);

  if (error && !overview) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="text-center">
          <p className="text-lg text-destructive">Connection failed</p>
          <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          <p className="mt-3 text-xs text-muted-foreground">
            Make sure <code className="text-foreground">looper serve</code> is running
          </p>
        </div>
      </div>
    );
  }

  if (!overview) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-muted-foreground">Connecting...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <div>
          <h1 className="text-2xl font-bold">Dashboard</h1>
          <p className="mt-1 text-sm text-muted-foreground">What is happening now</p>
        </div>
        {error && (
          <p role="alert" className="text-xs text-destructive">
            Cannot refresh ({error}); these are the last numbers read.
          </p>
        )}
        <Health unhealthy={overview.health.unhealthy} />
      </div>
      <StatusCard />
      <TaskCounts tasks={overview.tasks} />
      <DashboardGates needsYou={overview.tasks.needs_you} waitingGates={waitingGates} revision={overview} />
      <Agents overview={overview} />
      <ManagerMetricsSection />
      <TaskQualitySection />
      <Concurrency projects={overview.projects} idle={overview.idle_projects} />
      {overview.schedules.total > 0 && <Schedules schedules={overview.schedules} />}
      <Spend spend={overview.spend_today} />
    </div>
  );
}
