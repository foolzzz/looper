import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  FEEDBACK_NOTE_MAX,
  VERDICTS,
  fetchStepAttempts,
  fetchTaskFeedback,
  setTaskFeedback,
  type BasisCost,
  type Cost,
  type CostGroup,
  type StepAttempts,
  type TaskFeedback,
  type Verdict,
} from "@/lib/task-metrics";
import { V1Error } from "@/lib/v1";
import { formatUsd } from "@/lib/usage";

/** Why an attempt was not accepted, in words (the `failure_kind` of an attempt). */
export const FAILURE_LABELS: Record<string, string> = {
  review_rejected: "评审未通过",
  contract: "输出不符合契约",
  files_mismatch: "改动的文件与步骤不符",
  reported_failed: "Worker 自报失败",
  run_error: "运行出错",
  timeout: "运行超时",
  run_cap: "触到单次运行的花费上限",
  crash: "进程崩溃",
  task_budget: "任务预算用完",
  check_failed: "检查未通过",
  stopped: "任务被停止",
  none: "没有记录原因",
};

const VERDICT_LABELS: Record<Verdict, string> = {
  defect: "有缺陷",
  incomplete: "不完整",
  wrong_approach: "方案不对",
  other: "其他",
};

/** Money of one billing basis; an unknown cost reads 未知, never $0, and a partly unknown one carries a plus. */
export function Money({ cost }: { cost: BasisCost }) {
  if (cost.runs === 0) return <span>–</span>;
  if (cost.usd === null) return <span title={`${cost.unknown_runs} 次运行的花费未知`}>未知</span>;
  return cost.unknown_runs > 0 ? (
    <span title={`另有 ${cost.unknown_runs} 次运行的花费未知`}>{formatUsd(cost.usd)}+</span>
  ) : (
    <span>{formatUsd(cost.usd)}</span>
  );
}

const BASES = [
  { key: "provider_priced", label: "按供应商价格" },
  { key: "equivalent", label: "等价花费" },
] as const;

const SPLITS = [
  { key: "worker", label: "执行" },
  { key: "review", label: "评审" },
  { key: "advisor", label: "advisor" },
] as const;

function Split({ group, basis }: { group: CostGroup; basis: (typeof BASES)[number]["key"] }) {
  const parts = SPLITS.filter(({ key }) => group[key][basis].runs > 0);
  if (parts.length === 0) return null;
  return (
    <span className="mt-0.5 block text-xs text-muted-foreground">
      {parts.map(({ key, label }, i) => (
        <span key={key}>
          {i > 0 && " · "}
          {label} <Money cost={group[key][basis]} />
        </span>
      ))}
    </span>
  );
}

function CostTable({ costs }: { costs: StepAttempts["costs"] }) {
  const rows: { label: string; cost: Cost; group?: CostGroup }[] = [
    { label: "首次尝试", cost: costs.first.total, group: costs.first },
    { label: "重试", cost: costs.retry.total, group: costs.retry },
    { label: "规划", cost: costs.planning },
    { label: "未归属", cost: costs.unattributed.total, group: costs.unattributed },
    { label: "合计", cost: costs.total },
  ];
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-72 text-sm">
        <caption className="sr-only">花费拆分</caption>
        <thead>
          <tr className="text-left text-xs text-muted-foreground">
            <th scope="col" className="pb-1 pr-3 font-normal">花费</th>
            {BASES.map(({ key, label }) => (
              <th key={key} scope="col" className="pb-1 pr-3 text-right font-normal">{label}</th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map(({ label, cost, group }) => (
            <tr key={label} className={label === "合计" ? "font-medium" : undefined}>
              <th scope="row" className="py-1.5 pr-3 text-left font-normal">{label}</th>
              {BASES.map(({ key }) => (
                <td key={key} className="py-1.5 pr-3 text-right align-top font-mono tabular-nums">
                  <Money cost={cost[key]} />
                  {group && <Split group={group} basis={key} />}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Tile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="rounded-lg border border-border px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-bold tabular-nums">{value}</p>
      {detail && <p className="mt-0.5 text-xs text-muted-foreground">{detail}</p>}
    </div>
  );
}

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** The operator's mark that a completed task's delivery had a problem (`task_feedback`). It is shown here only: no
 * agent reads it. Without the operator's access the read is refused and the control is not shown. */
export function DeliveryFeedback({ taskId }: { taskId: string }) {
  const [feedback, setFeedback] = useState<TaskFeedback | null>(null);
  const [allowed, setAllowed] = useState(true);
  const [editing, setEditing] = useState(false);
  const [verdict, setVerdict] = useState<Verdict>("defect");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setFeedback(null);
    setAllowed(true);
    setEditing(false);
    fetchTaskFeedback(taskId, controller.signal)
      .then((f) => !controller.signal.aborted && setFeedback(f))
      .catch((e) => {
        if (controller.signal.aborted) return;
        if (e instanceof V1Error && e.status === 403) setAllowed(false);
        else setError(errorText(e));
      });
    return () => controller.abort();
  }, [taskId]);

  if (!allowed || (!feedback && !error)) return null;

  const save = async (next: Verdict | null) => {
    setBusy(true);
    setError("");
    try {
      setFeedback(await setTaskFeedback(taskId, next, note.trim()));
      setEditing(false);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const edit = () => {
    setVerdict(feedback?.verdict ?? "defect");
    setNote(feedback?.note ?? "");
    setEditing(true);
  };

  return (
    <div className="mt-4 border-t border-border pt-3" data-testid="delivery-feedback">
      {error && <p role="alert" className="mb-2 text-xs text-destructive">{error}</p>}
      {editing ? (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            void save(verdict);
          }}
        >
          <fieldset>
            <legend className="text-xs text-muted-foreground">交付有问题：原因</legend>
            <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
              {VERDICTS.map((v) => (
                <label key={v} className="inline-flex items-center gap-1.5 text-sm pointer-coarse:min-h-11">
                  <input type="radio" name={`verdict-${taskId}`} checked={verdict === v} onChange={() => setVerdict(v)} />
                  {VERDICT_LABELS[v]}
                </label>
              ))}
            </div>
          </fieldset>
          <label className="block text-xs text-muted-foreground">
            备注（可选，只在控制台显示，最多 {FEEDBACK_NOTE_MAX} 字）
            <textarea
              value={note}
              maxLength={FEEDBACK_NOTE_MAX}
              onChange={(e) => setNote(e.target.value)}
              rows={3}
              className="mt-1 block w-full rounded-md border border-border bg-transparent px-3 py-2 text-sm text-foreground pointer-coarse:text-base"
            />
          </label>
          <div className="flex gap-2">
            <Button type="submit" size="sm" disabled={busy}>保存</Button>
            <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => setEditing(false)}>取消</Button>
          </div>
        </form>
      ) : feedback?.verdict ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Badge variant="warning">交付有问题：{VERDICT_LABELS[feedback.verdict]}</Badge>
          {feedback.note && <span className="min-w-0 break-words text-sm">{feedback.note}</span>}
          <Button type="button" size="sm" variant="outline" onClick={edit}>修改</Button>
          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => void save(null)}>撤销标记</Button>
        </div>
      ) : (
        <Button type="button" size="sm" variant="outline" onClick={edit}>交付有问题</Button>
      )}
    </div>
  );
}

/** The 「指标」 card on a task's Overview: its attempts, what they cost and why some were not accepted
 * (`metrics.step_attempts`). `revision` changes whenever the page loads the task again, which reads the card again. */
export function TaskMetricsCard({ taskId, status, active, revision }: {
  taskId: string;
  status: string;
  active: boolean;
  revision: string;
}) {
  const [view, setView] = useState<StepAttempts | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    fetchStepAttempts(taskId, controller.signal)
      .then((v) => {
        if (controller.signal.aborted) return;
        setView(v);
        setError("");
      })
      .catch((e) => !controller.signal.aborted && setError(errorText(e)));
    return () => controller.abort();
  }, [taskId, revision]);

  const failures = view ? Object.entries(view.summary.failure_kinds) : [];
  return (
    <section aria-labelledby="task-metrics-title" className="rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 id="task-metrics-title" className="text-sm font-semibold">指标</h3>
        {active && <Badge variant="outline">进行中</Badge>}
      </div>
      {error && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          读取指标失败（{error}）{view ? "；显示的是上次读到的数字。" : "。"}
        </p>
      )}
      {!view ? (
        !error && <p className="mt-2 text-sm text-muted-foreground">读取中…</p>
      ) : view.summary.attempts === 0 ? (
        <p className="mt-2 text-sm text-muted-foreground">
          这个任务没有尝试记录：只有开始记录之后派出的步骤才有（{view.coverage_since ? "记录始于 " + new Date(view.coverage_since).toLocaleDateString() : "还没有任何记录"}）。
        </p>
      ) : (
        <div className="mt-3 space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Tile
              label="一次通过的步骤"
              value={`${view.summary.first_pass_steps} / ${view.summary.steps}`}
              detail={active ? "进行中，随运行更新" : undefined}
            />
            <Tile
              label="尝试次数"
              value={String(view.summary.attempts)}
              detail={view.summary.open_attempts > 0 ? `${view.summary.open_attempts} 次尚未决定` : undefined}
            />
            <Tile label="被接受的步骤" value={`${view.summary.accepted_steps} / ${view.summary.steps}`} />
          </div>
          <CostTable costs={view.costs} />
          <div>
            <h4 className="text-xs text-muted-foreground">没被接受的原因</h4>
            {failures.length === 0 ? (
              <p className="mt-1 text-sm text-muted-foreground">没有。</p>
            ) : (
              <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-sm">
                {failures.map(([kind, count]) => (
                  <li key={kind}>
                    {FAILURE_LABELS[kind] ?? kind} <span className="tabular-nums text-muted-foreground">×{count}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {view.missing_attempts > 0 && (
            <p role="alert" className="text-xs text-warning">
              有 {view.missing_attempts} 个 Worker 运行没有记下尝试（写入失败），上面的数字不完整。
            </p>
          )}
        </div>
      )}
      {status === "completed" && <DeliveryFeedback taskId={taskId} />}
    </section>
  );
}
