import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { callV1, confirmV1, type V1PendingConfirmation } from "@/lib/v1";

type Counter = { scope: string; scope_ref: string; currency: string; spent_usd: number; reserved_usd: number; limit_usd: number | null };
type Observation = { at?: string; run_kind: string; currency: string; estimate_usd: number; estimate_source: string;
  decision?: string; scope: string | null; scope_ref: string | null; action: string | null;
  would_hold_until?: string; note?: string };
type UsageHold = { provider: string; reason: string; until: string; window: string | null; reset_known: boolean };
type View = { mode: "off" | "observe" | "enforce"; daily_usd: number | null; daily_equivalent_usd: number | null;
  roles: Record<string, { daily_usd: number | null; daily_equivalent_usd: number | null }>;
  counters: Counter[]; recent: Observation[]; recommended_daily_usd: number | null;
  recommended_daily_equivalent_usd: number | null; problem: string | null; holds?: UsageHold[] };

const money = (value: number | null) => value === null ? "未设上限" : `$${value.toFixed(2)}`;
const currency = (value: string) => value === "equivalent_usd" ? "等价花费" : "真钱";
const ACTIONS: Record<string, string> = { ask: "需询问", stop_role: "停到明天", skip: "跳过这次", hold: "暂缓" };
const HOLD_REASONS: Record<string, string> = {
  rejected: "账号被拒", high_utilization: "用量过高", quota_failure: "配额失败", quota_cooldown: "配额冷却",
};
const modeLabel = (mode: View["mode"]) => mode === "off" ? "已关闭" : mode === "enforce" ? "执行中" : "仅观察";
function outcomeText(row: Observation): string {
  if (row.decision === "hold") return row.would_hold_until ? `暂缓到 ${row.would_hold_until}` : "暂缓";
  if (row.decision === "admit" && (row.would_hold_until || row.note)) {
    return ["本来会放行", row.would_hold_until, row.note].filter(Boolean).join(" · ");
  }
  if (row.decision === "admit") return "本来会放行";
  if (row.action) return ACTIONS[row.action] ?? row.action;
  return "超出上限";
}
const SOURCES: Record<string, string> = { run_cap: "单次上限", p95: "近 30 天 P95", floor: "角色下限", floor_no_samples: "无历史样本，使用下限" };
const SCOPES: Record<string, string> = { task: "任务", schedule_day: "定时任务当天", role_day: "角色当天", global_day: "全局当天" };

type Hold = { id: number; scope: string; scope_ref: string; currency: string; limit_usd: number;
  spent_usd: number; created_at: string; task_ids?: string[]; reason?: string };

/** One hold's explicit decision, including the existing operator confirmation when a cap is raised. */
function HoldDecision({ hold, onDecided }: { hold: Hold; onDecided: () => void }) {
  const [amount, setAmount] = useState("");
  const [pending, setPending] = useState<V1PendingConfirmation | null>(null);
  const [pendingDecision, setPendingDecision] = useState<"continue" | "stop">("continue");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function decide(decision: "continue" | "stop") {
    const value = amount.trim() ? Number(amount) : undefined;
    if (decision === "continue" && value !== undefined && (!Number.isFinite(value) || value < 0)) {
      setError("请填写有效的美元上限。"); return;
    }
    setBusy(true); setError("");
    try {
      const result = await callV1<Record<string, unknown>>("/api/v1/cost_holds/decide", {
        hold_id: hold.id, decision, ...(decision === "continue" && value !== undefined ? { new_limit_usd: value } : {}),
      });
      if (result.status === "needs_confirmation") { setPendingDecision(decision); setPending(result as V1PendingConfirmation); }
      else onDecided();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (!pending) return;
    setBusy(true); setError("");
    try {
      const result = await confirmV1<Record<string, unknown>>(pending.confirmation_id, pending.session);
      if (result.status === "needs_confirmation") setPending(result as V1PendingConfirmation);
      else { setPending(null); onDecided(); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  }
  return <li className="space-y-3 py-4 first:pt-0 last:pb-0">
    <p className="font-medium">C-{hold.id} · {SCOPES[hold.scope] ?? hold.scope} {hold.scope_ref}</p>
    <p className="text-sm text-muted-foreground">{currency(hold.currency)}已花及预留 {money(hold.spent_usd)} / 上限 {money(hold.limit_usd)}。{hold.task_ids?.length || hold.scope === "task" ? "24 小时未回答会停下任务。" : "24 小时未回答，上限不变。"}{hold.reason === "review_budget" && "被拦下的动作不会自动重发。"}</p>
    {pending ? <div className="space-y-2" role="status">
      <p className="text-sm">{pending.impact}</p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" disabled={busy} onClick={() => void confirm()}>{pendingDecision === "stop" ? "确认停下" : "确认提高上限"}</Button>
        <Button type="button" variant="outline" disabled={busy} onClick={() => setPending(null)}>取消</Button>
      </div>
    </div> : <div className="flex flex-wrap items-end gap-2">
      <label className="space-y-1 text-sm">新上限（美元）
        <Input className="w-40" aria-label={`C-${hold.id} 新上限（美元）`} inputMode="decimal" value={amount}
          onChange={(event) => setAmount(event.target.value)} placeholder="继续时可提高" disabled={busy} />
      </label>
      <Button type="button" disabled={busy} onClick={() => void decide("continue")}>继续</Button>
      <Button type="button" variant="outline" disabled={busy} onClick={() => void decide("stop")}>停</Button>
    </div>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </li>;
}

/** Settings → 花费: observations and the operator's pending spend decisions. */
export function CostGuardSettingsSection() {
  const [view, setView] = useState<View | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [holds, setHolds] = useState<Hold[] | null>(null);
  const [holdsError, setHoldsError] = useState("");
  async function readHolds() {
    try {
      const result = await callV1<{ holds: Hold[] }>("/api/v1/cost_holds/list", {});
      if ("status" in result) throw new Error("无法读取等待决定的花费。");
      setHolds(result.holds); setHoldsError("");
    } catch (reason) { setHoldsError(reason instanceof Error ? reason.message : String(reason)); }
  }
  useEffect(() => {
    let alive = true;
    callV1<View>("/api/v1/cost_guard/get", {}).then((result) => {
      if (!alive) return;
      if ("status" in result) setError("无法读取花费观察数据。");
      else setView(result);
    }, (reason: unknown) => alive && setError(reason instanceof Error ? reason.message : String(reason)));
    return () => { alive = false; };
  }, []);

  useEffect(() => { if (view) void readHolds(); }, [view?.mode]);

  if (error) return <p role="alert" className="text-sm text-destructive">{error}</p>;
  if (!view) return <p role="status" className="text-sm text-muted-foreground">正在读取花费观察…</p>;
  return <div className="space-y-6">
    {view.problem && <p role="alert" className="text-sm text-destructive">{view.problem}</p>}
    <Card>
      <CardHeader><CardTitle className="flex flex-wrap items-center gap-3">花费守卫 <Badge variant="outline">{modeLabel(view.mode)}</Badge></CardTitle></CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p>{view.mode === "enforce"
          ? "任务按已花和预留检查上限；选择询问时等待你决定，全局当天上限会暂停新的任务运行。范围链设有上限时，订阅运行会按预留金额事中软停；没有上限则不软停。fail 会计入预留，可能提前停止任务及正在运行的 Worker。收尾回合也按实际准入结果决定。金额上限拒绝优先于用量窗口暂缓。账号被拒或配额失败时，包括你自己发起的运行都会暂缓到重置；重置时间未知时最多冷却五分钟。95% 利用率只暂缓后台角色和定时触发。操作者的管家回合可越过金额上限，但仍受被拒窗口约束；不自动换账号。后台角色达到自身、定时任务或全局当天上限时停止；定时触发到上限时跳过这次。"
          : "先观察一周，再根据实际花费决定上限。目前只记录预留和本来会被拦下的运行，不拦截、不提问、不发消息。"}</p>
        <dl className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div><dt className="text-muted-foreground">全局每日真钱上限</dt><dd className="mt-1 font-mono">{money(view.daily_usd)}</dd></div>
          <div><dt className="text-muted-foreground">全局每日等价花费上限</dt><dd className="mt-1 font-mono">{money(view.daily_equivalent_usd)}</dd></div>
        </dl>
        {Object.keys(view.roles).length > 0 && <dl className="space-y-2">
          {Object.entries(view.roles).map(([role, limits]) => <div key={role} className="flex flex-wrap gap-x-4 gap-y-1">
            <dt>{role}</dt><dd>每日真钱 {money(limits.daily_usd)} · 等价花费 {money(limits.daily_equivalent_usd)}</dd>
          </div>)}
        </dl>}
        <p className="text-muted-foreground">等价花费是订阅用量按 API 价格折算的数字，不是真钱。没有价格和单次上限的运行只在开始前估算，无法在运行中限额。</p>
        {(view.recommended_daily_usd !== null || view.recommended_daily_equivalent_usd !== null)
          ? <p>近 30 天每日花费 P95 × 1.5 建议值（须操作者确认）：真钱 {money(view.recommended_daily_usd)}；等价花费 {money(view.recommended_daily_equivalent_usd)}。</p>
          : <p className="text-muted-foreground">观察满一周后才提供每日上限建议；建议不会自动生效。</p>}
      </CardContent>
    </Card>
    {view.holds && view.holds.length > 0 && <Card>
      <CardHeader><CardTitle>用量窗口暂缓</CardTitle></CardHeader>
      <CardContent>
        <ul className="divide-y divide-border text-sm">{view.holds.map((hold) => <li key={`${hold.provider}:${hold.window ?? "failure"}:${hold.until}`} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-3 first:pt-0 last:pb-0">
          <Badge variant="warning">{HOLD_REASONS[hold.reason] ?? hold.reason}</Badge>
          <span>{hold.provider}{hold.window ? ` · ${hold.window}` : " · 失败记录"}</span>
          <span className="font-mono text-xs">{hold.until}{hold.reset_known ? "" : " · 重置时间未知"}</span>
        </li>)}</ul>
      </CardContent>
    </Card>}
    <Card>
      <CardHeader><CardTitle>花费等你决定</CardTitle></CardHeader>
      <CardContent>
        <p className="mb-3 text-sm text-muted-foreground">已有等待在关闭或仅观察模式下仍可处理；任务等待 24 小时未答复会停下，其他等待到期后上限不变。</p>
        {holdsError ? <div role="alert" className="space-y-2 text-sm text-destructive"><p>{holdsError}</p>
          <Button type="button" variant="outline" onClick={() => void readHolds()}>重试</Button></div>
          : holds === null ? <p role="status" className="text-sm text-muted-foreground">正在读取等待决定的花费…</p>
          : holds.length === 0 ? <p className="text-sm text-muted-foreground">没有等待决定的花费。</p>
          : <ul className="divide-y divide-border">{holds.map((hold) => <HoldDecision key={hold.id} hold={hold} onDecided={() => void readHolds()} />)}</ul>}
      </CardContent>
    </Card>
    <Card>
      <CardHeader><CardTitle>今天已花与预留</CardTitle></CardHeader>
      <CardContent>
        {view.counters.length === 0 ? <p className="text-sm text-muted-foreground">暂无计数；关闭时不收集守卫数据。</p>
          : <div className="overflow-x-auto"><table className="w-full text-left text-sm">
            <thead><tr className="border-b border-border text-muted-foreground"><th className="pb-2 pr-4">范围</th><th className="pb-2 pr-4">币种</th><th className="pb-2 pr-4">已花</th><th className="pb-2 pr-4">预留</th><th className="pb-2">上限</th></tr></thead>
            <tbody>{view.counters.map((row) => <tr key={`${row.scope}:${row.scope_ref}:${row.currency}`} className="border-b border-border last:border-0">
              <td className="py-3 pr-4">{SCOPES[row.scope] ?? row.scope} {row.scope_ref}</td><td className="py-3 pr-4">{currency(row.currency)}</td>
              <td className="py-3 pr-4 font-mono">{money(row.spent_usd)}</td><td className="py-3 pr-4 font-mono">{money(row.reserved_usd)}</td><td className="py-3 font-mono">{money(row.limit_usd)}</td>
            </tr>)}</tbody>
          </table></div>}
      </CardContent>
    </Card>
    <Card>
      <CardHeader><CardTitle>运行观察（最近 20 条）</CardTitle></CardHeader>
      <CardContent>
        {view.recent.length === 0 ? <p className="text-sm text-muted-foreground">暂无观察记录。</p>
          : <ul className="divide-y divide-border text-sm">{view.recent.slice(0, 20).map((row, index) => <li key={`${row.at}:${index}`} className="space-y-1 py-3 first:pt-0 last:pb-0">
            <p>{row.run_kind} · {currency(row.currency)} {money(row.estimate_usd)} · {outcomeText(row)}</p>
            <p className="text-xs text-muted-foreground">{SCOPES[row.scope ?? ""] ?? row.scope} {row.scope_ref} · {SOURCES[row.estimate_source] ?? row.estimate_source}{row.at ? ` · ${new Date(row.at).toLocaleString()}` : ""}</p>
          </li>)}</ul>}
      </CardContent>
    </Card>
  </div>;
}
