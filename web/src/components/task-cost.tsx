import type { TaskCost, TaskRole } from "@/lib/api";
import { costBasis, costCaveats, formatUsd } from "@/lib/usage";

const ROLES: [TaskRole, string][] = [
  ["planner", "Planner"],
  ["worker", "Workers"],
  ["reviewer", "Reviewers"],
];

function runs(n: number) {
  return `${n} run${n === 1 ? "" : "s"}`;
}

/** A task's cost by the agents that ran: each role's share of the total, its runs and its cost. */
export function TaskCostBreakdown({ cost }: { cost: TaskCost }) {
  const caveats = [costBasis(cost), ...costCaveats(cost)].filter(Boolean);
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">Cost by agent</h3>
        <p className="font-mono text-xs text-muted-foreground">
          {formatUsd(cost.cost_usd)} · {runs(cost.runs)}
          {caveats.length > 0 && ` · ${caveats.join(", ")}`}
        </p>
      </div>
      <div className="mt-2 space-y-1.5">
        {ROLES.map(([role, label]) => {
          const share = cost.by_role[role];
          const percent = cost.cost_usd > 0 ? (share.cost_usd / cost.cost_usd) * 100 : 0;
          return (
            <div key={role} className="grid grid-cols-[5.5rem_1fr_auto] items-center gap-3 text-xs">
              <span className="text-muted-foreground">{label}</span>
              <div
                className="h-1.5 overflow-hidden rounded-full bg-muted"
                role="meter"
                aria-label={`${label}: share of the task's cost`}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(percent)}
              >
                <div className="h-full rounded-full bg-primary/70" style={{ width: `${percent}%` }} />
              </div>
              <span className="text-right font-mono tabular-nums">
                <span className="text-muted-foreground">{runs(share.runs)} · </span>
                {formatUsd(share.cost_usd)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
