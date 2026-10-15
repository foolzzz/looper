import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { fetchOverview, TASK_GROUPS, type LiveStepRun, type Overview, type TaskGroup } from "@/lib/api";
import { historyHref } from "@/pages/agent-history";
import { formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";

/** The groups always counted; "Cancelled" and "Other" show only when they hold a task. */
const ALWAYS_SHOWN: TaskGroup[] = ["in_progress", "queued", "ready", "attention", "completed"];

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
            <span className="font-medium text-destructive">{c.name} is unhealthy.</span>{" "}
            {c.max_restarts === 0
              ? "It is not restarted automatically."
              : c.restart_count >= c.max_restarts
                ? `Restarted ${c.restart_count} of ${c.max_restarts} times; it is not restarted again until looper serve restarts.`
                : `Restarted ${c.restart_count} of ${c.max_restarts} times so far.`}
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
          count === 0 ? "" : g.id === "in_progress" ? "text-primary" : g.id === "attention" ? "text-warning" : "";
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
              {p.requirement.split("\n")[0]}
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
  load,
  waiting,
  extra,
}: {
  label: string;
  load: { running: number; limit: number };
  waiting: number;
  extra?: string;
}) {
  const full = load.running >= load.limit;
  return (
    <span className="whitespace-nowrap tabular-nums">
      <span className="text-muted-foreground">{label} </span>
      <span className={cn(full && "text-warning")}>
        {load.running}/{load.limit}
      </span>
      {full && <span className="text-warning"> full</span>}
      {waiting > 0 && <span className="text-muted-foreground"> · {waiting} waiting</span>}
      {extra && <span className="text-muted-foreground"> · {extra}</span>}
    </span>
  );
}

function Concurrency({ projects, idle }: { projects: Overview["projects"]; idle: number }) {
  return (
    <Section title="Concurrency by project" meta="running / limit">
      {projects.length === 0 ? (
        <p className="text-sm text-muted-foreground">No project has a task in progress, queued or ready.</p>
      ) : (
        <ul className="divide-y divide-border">
          {projects.map((p) => (
            <li key={p.project_id} className="flex flex-wrap items-baseline gap-x-5 gap-y-1 py-2 text-sm first:pt-0 last:pb-0">
              <a href={`#projects/${encodeURIComponent(p.project_id)}`} className={cn("min-w-0 max-w-full truncate font-medium sm:w-40", linkClass)}>
                {p.name}
              </a>
              <Load label="tasks" load={p.tasks} waiting={p.tasks.queued} extra={p.tasks.ready ? `${p.tasks.ready} ready` : undefined} />
              <Load label="workers" load={p.workers} waiting={p.workers.waiting} />
              <Load label="reviewers" load={p.reviewers} waiting={p.reviewers.waiting} />
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
      <Spend spend={overview.spend_today} />
    </div>
  );
}
