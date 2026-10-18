import type { TaskCost, TaskRole } from "@/lib/api";
import { costBasis, costCaveats, formatTokens, formatUsd, modelRunCaveats } from "@/lib/usage";

const ROLES: [TaskRole, string][] = [
  ["planner", "Planner"],
  ["worker", "Workers"],
  ["reviewer", "Reviewers"],
];

function runs(n: number) {
  return `${n} run${n === 1 ? "" : "s"}`;
}

/** A task's cost by the agents that ran (each role's share of the total, its runs and its cost) and by model (its runs, tokens and cost, then the Manager's turns about the task, which the cost does not count). */
export function TaskCostBreakdown({ cost }: { cost: TaskCost }) {
  const caveats = [costBasis(cost), ...costCaveats(cost)].filter(Boolean);
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold">Cost by agent and model</h3>
        <p className="font-mono text-xs text-muted-foreground">
          {formatUsd(cost.cost_usd)} · {runs(cost.runs)}
          {caveats.length > 0 && ` · ${caveats.join(", ")}`}
        </p>
      </div>
      <div className="mt-2 grid gap-4 2xl:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <div className="space-y-1.5">
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
        {(cost.by_model.length > 0 || cost.manager_by_model.length > 0) && (
          <div className="min-w-0">
            <h4 className="text-xs font-medium text-muted-foreground">By model</h4>
            <div className="mt-2 overflow-x-auto">
              <table className="w-full whitespace-nowrap text-left text-xs">
                <thead className="text-muted-foreground">
                  <tr>
                    {["Model", "Runs", "Input", "Output", "Cache read", "Cache write", "Cost"].map((label, i) => (
                      <th key={label} scope="col" className={`pb-1 pr-3 font-medium ${i ? "text-right" : ""}`}>{label}</th>
                    ))}
                  </tr>
                </thead>
                {[cost.by_model, cost.manager_by_model].map((rows, group) => rows.length > 0 && (
                  <tbody key={group} className={group ? "border-t border-border" : undefined}>
                    {rows.map((row, i) => {
                      const caveats = modelRunCaveats(row);
                      return (
                        <tr key={i}>
                          <td className="py-1 pr-3">
                            <span className={row.model === null ? "text-muted-foreground" : "font-mono"}>
                              {group ? "Manager · " : ""}{row.model ?? "Not recorded"}
                            </span>
                            {(group > 0 || caveats) && (
                              <div className="text-[11px] text-muted-foreground">
                                {[group ? "Not counted in the task's cost or spend cap" : "", caveats].filter(Boolean).join(" · ")}
                              </div>
                            )}
                          </td>
                          <td className="py-1 pr-3 text-right font-mono tabular-nums">{row.runs}</td>
                          {[row.input_tokens, row.output_tokens, row.cache_read_tokens, row.cache_creation_tokens].map((value, j) => (
                            <td key={j} className="py-1 pr-3 text-right font-mono tabular-nums">
                              {formatTokens(value)}
                            </td>
                          ))}
                          <td className="py-1 pr-3 text-right font-mono tabular-nums">{formatUsd(row.cost_usd)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                ))}
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
