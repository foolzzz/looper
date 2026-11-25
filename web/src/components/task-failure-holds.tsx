import { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { fetchFailureHistory, fetchFailureHolds, isConfirmation, releaseFailureHold,
  type FailureHold, type HoldRelease, type StepFailure } from "@/lib/api-task-failures";
import { V1Error, type V1PendingConfirmation } from "@/lib/v1";

const names = { quota: "额度等待", transient: "故障退避", environment: "环境问题", breaker: "连续失败熔断" };
const actions = { retry: "已修好，重试", planner: "交给 Planner", stop: "停止任务" } as const;
const failureText = (err: unknown) => err instanceof V1Error && err.code === "unavailable" ? `服务暂时不可用：${err.message}` : String(err);
const at = (value: string | null) => value ? new Date(value).toLocaleString() : "等待操作者处理";

export function TaskFailureHolds({ taskId, holds: initial, onChanged }: {
  taskId: string; holds: FailureHold[]; onChanged: () => void;
}) {
  const [holds, setHolds] = useState(initial);
  const [history, setHistory] = useState<StepFailure[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{ request: HoldRelease; confirmation: V1PendingConfirmation } | null>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const origin = useRef<HTMLButtonElement | null>(null);
  useEffect(() => setHolds(initial), [initial]);
  const load = useCallback(async (after?: number) => {
    setLoading(true); setError("");
    try {
      const page = await fetchFailureHistory(taskId, { ...(after === undefined ? {} : { after_id: after }) });
      setHistory(previous => after === undefined ? page.items : [...previous, ...page.items]);
      setCursor(page.next_after_id);
    } catch (err) { setError(failureText(err)); }
    finally { setLoading(false); }
  }, [taskId]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (pending) cancel.current?.focus(); else origin.current?.focus(); }, [pending]);
  const refresh = async () => { setHolds((await fetchFailureHolds(taskId)).holds); onChanged(); };
  const release = async (request: HoldRelease, confirmation?: V1PendingConfirmation) => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const result = await releaseFailureHold(request, confirmation);
      if (isConfirmation(result)) setPending({ request, confirmation: result });
      else { setPending(null); await refresh(); await load(); }
    } catch (err) {
      setError(failureText(err)); setPending(null);
      if (err instanceof V1Error && ["conflict", "not_found"].includes(err.code)) {
        try { await refresh(); } catch (refreshError) { setError(`${String(err)}；${String(refreshError)}`); }
      }
    } finally { setBusy(false); }
  };
  return <section className="space-y-4" aria-label="任务失败处理">
    {holds.length > 0 && <Card><CardHeader><CardTitle>运行暂停 · {holds.length} 个步骤</CardTitle></CardHeader>
      <CardContent className="space-y-4">{holds.map(hold => <div key={hold.step_id} className="space-y-2 border-b border-border pb-4 last:border-0 last:pb-0">
        <div className="flex flex-wrap items-center gap-2"><Badge variant={hold.kind === "environment" || hold.kind === "breaker" ? "warning" : "outline"}>{names[hold.kind]}</Badge>
          <span className="break-all font-mono text-sm">{hold.step_key || hold.step_id}</span><span className="text-sm">{hold.role} · {hold.cause} · 连续 {hold.streak} 次失败</span></div>
        <p className="text-xs text-muted-foreground">开始：{at(hold.created_at)} · 恢复：{at(hold.until)}</p>
        <p className="text-xs text-muted-foreground">重试只恢复失败的执行单元；交给 Planner 做判断，或停止整个任务。操作者明确重启任务可清零连续失败计数。</p>
        <div className="flex flex-wrap gap-2">{Object.entries(actions).map(([action, label]) => <Button key={action} variant="outline" disabled={busy || pending !== null}
          onClick={event => { origin.current = event.currentTarget; void release({ task_id: taskId, step_id: hold.step_id, failure_id: hold.failure_id, action: action as HoldRelease["action"] }); }}>{label}</Button>)}</div>
      </div>)}</CardContent></Card>}
    {pending && <div role="group" aria-label="确认失败处理" className="space-y-3 rounded-md border border-border p-4">
      <p className="whitespace-pre-wrap break-words text-sm">{pending.confirmation.impact}</p>
      <div className="flex flex-wrap gap-2"><Button disabled={busy} onClick={() => void release(pending.request, pending.confirmation)}>确认</Button>
        <Button ref={cancel} variant="outline" disabled={busy} onClick={() => setPending(null)}>取消</Button></div></div>}
    <details className="rounded-md border border-border p-4"><summary className="cursor-pointer text-sm font-medium">失败历史{history.length ? ` · 已加载 ${history.length} 次` : ""}</summary>
      <div className="mt-3 space-y-3">{history.map(row => <div key={row.id} data-failure-id={row.id} className="space-y-1 border-b border-border pb-2 text-sm">
        <p className="break-words">{row.step_key || row.step_identity} · {row.role} · {row.failure_class} / {row.failure_cause} · 连续 {row.streak} 次 · {row.action}</p>
        <p className="text-xs text-muted-foreground">{at(row.created_at)} · 运行 {row.run_id}{row.record_id ? ` · 记录 ${row.record_id}` : ""}</p>
      </div>)}{loading && <p role="status" className="text-sm text-muted-foreground">正在读取历史…</p>}
      {!loading && !error && history.length === 0 && <p className="text-sm text-muted-foreground">尚无失败历史。</p>}
      {cursor !== null && <Button variant="outline" disabled={loading} onClick={() => void load(cursor)}>加载更多</Button>}</div>
    </details>
    {error && <div className="space-y-2"><p role="alert" className="break-words text-sm text-destructive">{error}</p><Button variant="outline" disabled={loading || busy} onClick={() => void load()}>重新读取</Button></div>}
  </section>;
}
