import { TaskCostBreakdown } from "@/components/task-cost";
import { TaskSpendMeter } from "@/components/task-budget";
import type { TaskDetail } from "@/lib/api";
import { costBasis, costCaveats, formatUsd } from "@/lib/usage";

/** Read only the shared detail; totals and role shares are supplied by the server. */
export function WorkCost({ task }: { task: Pick<TaskDetail, "cost" | "task_budget_usd" | "own_budget_usd"> }) {
  const { cost, task_budget_usd: cap, own_budget_usd: ownCap } = task;
  const recorded = cost && cost.runs > 0 ? cost : null;
  const partial = !!recorded && (recorded.running > 0 || recorded.unpriced_runs > 0);
  // An all-unknown aggregate has a zero subtotal, not a measured zero cost.
  const subtotal = recorded && Number.isFinite(recorded.cost_usd) && (!partial || recorded.cost_usd > 0
    || recorded.runs > recorded.running + recorded.unpriced_runs
    || recorded.by_model.some(row => row.cost_usd != null));
  const taskManager = recorded?.by_role.task_manager;

  return (
    <section aria-labelledby="work-cost-title" className="min-w-0 max-w-full space-y-2 overflow-x-auto p-4">
      <h2 id="work-cost-title" className="text-sm font-semibold">费用与上限</h2>
      {subtotal ? <>
        {partial && <p className="text-xs text-muted-foreground">已知费用小计，部分运行费用尚未确定</p>}
        <TaskCostBreakdown cost={recorded} />
        {taskManager && taskManager.runs > 0 && <p className="text-xs text-muted-foreground">
          任务管家 · {taskManager.runs} 次运行 · {formatUsd(taskManager.cost_usd)}（已含在上述小计中）
        </p>}
      </> : <>
        <p className="text-sm text-muted-foreground">费用尚不可用</p>
        {recorded && <p className="text-xs text-muted-foreground">
          {[costBasis(recorded), ...costCaveats(recorded)].filter(Boolean).join(" · ")}
        </p>}
      </>}
      <p className="text-xs text-muted-foreground">
        {cap == null ? "未设置上限" : <>
          {cap === 0 ? "上限为0" : `上限 ${formatUsd(cap)}`} · {ownCap != null ? "本任务单独设置" : "沿用项目每任务上限"}
        </>}
      </p>
      {cap != null && cap > 0 && subtotal && !partial && <TaskSpendMeter spent={recorded.cost_usd} cap={cap} />}
      {partial && cap != null && cap > 0 && <p className="text-xs text-muted-foreground">费用未完整，无法确定剩余额度</p>}
    </section>
  );
}
