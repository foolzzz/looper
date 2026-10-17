import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { RunStatusBadge, ScheduleTime } from "@/components/project-schedules";
import {
  fetchOverview,
  TASK_GROUPS,
  type LiveStepRun,
  type ComponentHealth,
  type Overview,
  type OverviewSchedules,
  type TaskGroup,
} from "@/lib/api";
import { historyHref } from "@/pages/agent-history";
import { formatUsd } from "@/lib/usage";
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
            href={`#tasks?group=${g.id}`}
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

function AgentRow({ role, count, children }: { role: string; count?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-3 py-2.5 first:pt-0 last:pb-0">
      <span className="text-sm font-medium">{role}</span>
      <div className="min-w-0 space-y-1">
        {count !== undefined && <p className="text-sm tabular-nums">{count}</p>}
        {children}
      </div>
    </div>
  );
}

function StepRuns({ runs }: { runs: LiveStepRun[] }) {
  return runs.map((run) => (
    <p key={run.agent_id} className="flex min-w-0 items-baseline gap-2 text-xs">
      {/* Listed under Workers or Reviewers, a run as a role its task's Planner defined shows the role's id. */}
      {run.task_role && <span className="shrink-0 font-mono text-foreground">{run.task_role.id}</span>}
      <a
        href={historyHref(run.agent_id, run.task_id)}
        title={[run.project_name, run.step].filter(Boolean).join(" · ") || undefined}
        className={cn("min-w-0 truncate text-muted-foreground", linkClass)}
      >
        {run.instruction.split("\n")[0] || run.step || run.agent_id}
      </a>
      {run.state === "waiting" ? (
        <Badge variant="warning" className="shrink-0">waiting</Badge>
      ) : (
        <span className="shrink-0 tabular-nums text-muted-foreground">{elapsed(run.elapsed_seconds)}</span>
      )}
    </p>
  ));
}

function runCount(runs: LiveStepRun[]): string {
  const waiting = runs.filter((r) => r.state === "waiting").length;
  return `${runs.length - waiting}${waiting ? ` running · ${waiting} waiting` : ""}`;
}

function Agents({ agents }: { agents: Overview["agents"] }) {
  const { manager, planners, workers, reviewers } = agents;
  return (
    <Section title="Agents running now">
      <div className="divide-y divide-border">
        <AgentRow role="Manager">
          {manager ? (
            <a href={historyHref(manager.agent_id)} className={cn("inline-flex items-center gap-1.5 text-sm", linkClass)}>
              {manager.state === "busy" ? (
                <>
                  <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" aria-hidden="true" />
                  busy
                </>
              ) : (
                <span className="text-muted-foreground">idle</span>
              )}
            </a>
          ) : (
            <p className="text-sm text-muted-foreground">unknown</p>
          )}
        </AgentRow>
        <AgentRow role="Planners" count={planners.length}>
          {planners.map((p) => (
            <a
              key={p.agent_id}
              href={historyHref(p.agent_id, p.task_id)}
              className={cn("block truncate text-xs text-muted-foreground", linkClass)}
            >
              {p.project_name ? `${p.project_name}: ` : ""}
              {p.name || p.requirement.split("\n")[0]}
            </a>
          ))}
        </AgentRow>
        <AgentRow role="Workers" count={runCount(workers)}>
          <StepRuns runs={workers} />
        </AgentRow>
        <AgentRow role="Reviewers" count={runCount(reviewers)}>
          <StepRuns runs={reviewers} />
        </AgentRow>
      </div>
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

export function DashboardPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const load = () =>
      fetchOverview()
        .then((o) => {
          setOverview(o);
          setError("");
        })
        .catch((e) => setError(e.message));
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
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
      <TaskCounts tasks={overview.tasks} />
      <Agents agents={overview.agents} />
      <Concurrency projects={overview.projects} idle={overview.idle_projects} />
      {overview.schedules.total > 0 && <Schedules schedules={overview.schedules} />}
      <Spend spend={overview.spend_today} />
    </div>
  );
}
