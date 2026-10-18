import { useEffect, useState } from "react";
import { fetchProjectRoleStats, type ProjectRoleStats, type RoleStats } from "@/lib/api";
import { formatUsd } from "@/lib/usage";

const percent = (rate: number | null) => (rate == null ? "—" : `${Math.round(rate * 100)}%`);

/** A role's numbers on one wrapping line, the same for the built-in Worker and Reviewer, so they compare. */
export function RoleStatsLine({ stats }: { stats: RoleStats }) {
  const reviews = stats.reviews.passed + stats.reviews.not_passed;
  const items: [string, string][] = [
    ["Runs", stats.running ? `${stats.runs} (${stats.running} running)` : String(stats.runs)],
    ["Cost", formatUsd(stats.cost_usd)],
    ["Steps", `${stats.accepted}/${stats.steps} accepted, ${stats.failed} failed`],
    ["Retries", String(stats.retries)],
    ["Contract violations", String(stats.contract_violations)],
    ["Review pass rate", reviews ? `${percent(stats.review_pass_rate)} of ${reviews}` : "—"],
  ];
  return (
    <span className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
      {items.map(([label, value]) => (
        <span key={label}>
          <span className="text-muted-foreground">{label}</span> <span className="font-mono tabular-nums">{value}</span>
        </span>
      ))}
    </span>
  );
}

/** The project's roles summed across its tasks, then the built-in Worker's and Reviewer's numbers in those tasks. */
export function ProjectRoleSummary({ projectId }: { projectId: string }) {
  const [data, setData] = useState<ProjectRoleStats | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    fetchProjectRoleStats(projectId)
      .then((d) => alive && setData(d))
      .catch((err) => alive && setError(err instanceof Error ? err.message : String(err)));
    return () => {
      alive = false;
    };
  }, [projectId]);
  if (error) return <p role="alert" className="text-sm text-destructive">{error}</p>;
  if (!data) return null;
  const builtin: [string, RoleStats][] = [
    ["Built-in Worker", data.builtin.worker],
    ["Built-in Reviewer", data.builtin.reviewer],
  ];
  return (
    <section aria-labelledby="project-role-summary" className="rounded-lg border border-border bg-card">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-5 py-3">
        <h3 id="project-role-summary" className="text-sm font-semibold">
          Roles across tasks
        </h3>
        <p className="text-[11px] text-muted-foreground">
          {data.tasks
            ? `Summed by id over the ${data.tasks} task${data.tasks === 1 ? "" : "s"} whose Planner defined roles; one id may read differently in each task.`
            : "No task of this project has defined a role yet."}
        </p>
      </div>
      {data.tasks > 0 && (
        <ul className="divide-y divide-border border-t border-border">
          {data.roles.map((role) => (
            <li key={role.id} className="space-y-1 px-5 py-3">
              <p className="flex flex-wrap items-center gap-x-2 text-xs">
                <span className="break-all font-mono text-sm font-semibold">{role.id}</span>
                <span className="text-muted-foreground">
                  {role.kinds.map((k) => (k === "work" ? "Work" : "Review")).join(" / ")} · {role.tasks} task
                  {role.tasks === 1 ? "" : "s"}
                  {role.templates.length > 0 && ` · from template ${role.templates.join(", ")}`}
                </span>
              </p>
              <RoleStatsLine stats={role.stats} />
            </li>
          ))}
          {builtin.map(([name, stats]) => (
            <li key={name} className="space-y-1 px-5 py-3">
              <p className="text-xs">
                <span className="text-sm font-semibold">{name}</span>
                <span className="text-muted-foreground"> · for comparison, in the same tasks</span>
              </p>
              <RoleStatsLine stats={stats} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
