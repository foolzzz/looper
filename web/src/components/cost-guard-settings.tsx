import { useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  COST_GUARD_ROLES, fetchCostGuard, fetchCostHolds, updateCostGuard,
  type CostGuardMode, type CostGuardPatch, type CostGuardRole, type CostGuardView, type CostHold,
} from "@/lib/api-cost-guard";
import { SessionEndedError } from "@/lib/session";
import { callV1, confirmV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

const money = (value: number | null | undefined) =>
  typeof value === "number" && Number.isFinite(value) ? `$${value.toFixed(2)}` : "未设上限";
const currency = (value: string) => value === "equivalent_usd" ? "等价花费" : "真钱";
const ACTIONS: Record<string, string> = { ask: "需询问", stop_role: "停到明天", skip: "跳过这次", hold: "暂缓" };
const HOLD_REASONS: Record<string, string> = {
  rejected: "账号被拒", high_utilization: "用量过高", quota_failure: "配额失败", quota_cooldown: "配额冷却",
};
const modeLabel = (mode: CostGuardView["mode"]) => mode === "off" ? "已关闭" : mode === "enforce" ? "执行中" : "仅观察";
function outcomeText(row: CostGuardView["recent"][number]): string {
  if (row.decision === "hold") return row.would_hold_until ? `暂缓到 ${row.would_hold_until}` : "暂缓";
  if (row.decision === "admit" && (row.would_hold_until || row.note)) {
    return ["本来会放行", row.would_hold_until, row.note].filter(Boolean).join(" · ");
  }
  if (row.decision === "admit") return "本来会放行";
  if (row.action) return ACTIONS[row.action] ?? row.action;
  if (!row.decision) return "已记录";
  return "超出上限";
}
const SOURCES: Record<string, string> = { run_cap: "单次上限", p95: "近 30 天 P95", floor: "角色下限", floor_no_samples: "无历史样本，使用下限" };
const SCOPES: Record<string, string> = { task: "任务", schedule_day: "定时任务当天", role_day: "角色当天", global_day: "全局当天",
  schedule: "定时任务" };
const CAPS = ["daily_usd", "daily_equivalent_usd"] as const;
const FIELD = "block space-y-1 text-sm pointer-coarse:text-base";
const SELECT = "flex h-9 w-full rounded-md border border-input bg-transparent px-3 text-sm text-foreground shadow-sm pointer-coarse:h-11 pointer-coarse:text-base focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50";

function unansweredHold(hold: CostHold): boolean {
  return hold.reason === "unanswered"
    || (hold.currency == null && hold.limit_usd == null && hold.spent_usd == null && hold.pushes != null);
}

function stopped(error: unknown): boolean {
  return error instanceof SessionEndedError || (error instanceof DOMException && error.name === "AbortError");
}

/** Blank clears a cap. `0` is a cap. Anything that is not a finite non-negative number is rejected. */
function parseAmount(raw: string): number | null | undefined {
  const text = raw.trim();
  if (text === "") return null;
  if (!/^(?:\d+\.?\d*|\.\d+)$/.test(text)) return undefined;
  const value = Number(text);
  if (!Number.isFinite(value) || value < 0) return undefined;
  return value;
}

type Draft = {
  mode: CostGuardMode;
  daily_usd: string;
  daily_equivalent_usd: string;
  roles: Record<CostGuardRole, { daily_usd: string; daily_equivalent_usd: string }>;
};

const textOf = (value: number | null | undefined) => (value == null ? "" : String(value));

function draftFrom(view: CostGuardView): Draft {
  return {
    mode: view.mode,
    daily_usd: textOf(view.daily_usd),
    daily_equivalent_usd: textOf(view.daily_equivalent_usd),
    roles: Object.fromEntries(COST_GUARD_ROLES.map((role) => [role, {
      daily_usd: textOf(view.roles[role]?.daily_usd),
      daily_equivalent_usd: textOf(view.roles[role]?.daily_equivalent_usd),
    }])) as Draft["roles"],
  };
}

function changedPatch(view: CostGuardView, draft: Draft): { valid: boolean; dirty: boolean; patch: CostGuardPatch | null } {
  const patch: CostGuardPatch = {};
  let valid = true;
  let dirty = false;
  if (draft.mode !== view.mode) { dirty = true; patch.mode = draft.mode; }
  for (const cap of CAPS) {
    const value = parseAmount(draft[cap]);
    if (value === undefined) { valid = false; dirty = true; }
    else if (value !== view[cap]) { dirty = true; patch[cap] = value; }
  }
  const roles: NonNullable<CostGuardPatch["roles"]> = {};
  for (const role of COST_GUARD_ROLES) {
    const saved = view.roles[role];
    const rolePatch: Partial<CostGuardView["roles"][CostGuardRole]> = {};
    for (const cap of CAPS) {
      const value = parseAmount(draft.roles[role][cap]);
      if (value === undefined) { valid = false; dirty = true; }
      else if (value !== (saved?.[cap] ?? null)) { dirty = true; rolePatch[cap] = value; }
    }
    if (rolePatch.daily_usd !== undefined || rolePatch.daily_equivalent_usd !== undefined) roles[role] = rolePatch;
  }
  if (Object.keys(roles).length > 0) patch.roles = roles;
  return { valid, dirty, patch: valid && dirty ? patch : null };
}

function isPending(value: unknown): value is V1PendingConfirmation {
  return Boolean(value) && typeof value === "object" && (value as { status?: string }).status === "needs_confirmation";
}

function messageOf(error: unknown): string {
  if (error instanceof V1Error && error.field) return `${error.field}: ${error.message}`;
  return error instanceof Error ? error.message : String(error);
}

/** One hold's explicit decision, including the existing operator confirmation when a cap is raised. */
function HoldDecision({ hold, onDecided }: { hold: CostHold; onDecided: () => void }) {
  const unanswered = unansweredHold(hold);
  const [amount, setAmount] = useState("");
  const [pending, setPending] = useState<V1PendingConfirmation | null>(null);
  const [pendingDecision, setPendingDecision] = useState<"continue" | "stop">("continue");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function decide(decision: "continue" | "stop") {
    const value = !unanswered && amount.trim() ? Number(amount) : undefined;
    if (decision === "continue" && value !== undefined && (!Number.isFinite(value) || value < 0)) {
      setError("请填写有效的美元上限。"); return;
    }
    setBusy(true); setError("");
    try {
      const result = await callDecide(hold.id, decision, value);
      if (isPending(result)) { setPendingDecision(decision); setPending(result); }
      else onDecided();
    } catch (reason) { if (!stopped(reason)) setError(messageOf(reason)); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (!pending) return;
    setBusy(true); setError("");
    try {
      const result = await updateHold(pending);
      if (isPending(result)) setPending(result);
      else { setPending(null); onDecided(); }
    } catch (reason) { if (!stopped(reason)) setError(messageOf(reason)); }
    finally { setBusy(false); }
  }
  return <li className="space-y-3 py-4 first:pt-0 last:pb-0">
    <p className="font-medium">C-{hold.id} · {SCOPES[hold.scope] ?? hold.scope} {hold.scope_ref}</p>
    <p className="text-sm text-muted-foreground">{unanswered
      ? `${hold.pushes ?? ""} 条推送没人回。继续不需要填写上限；停则暂停这个定时任务。`
      : <>{currency(String(hold.currency ?? ""))}已花及预留 {money(typeof hold.spent_usd === "number" ? hold.spent_usd : null)} / 上限 {money(typeof hold.limit_usd === "number" ? hold.limit_usd : null)}。{hold.task_ids?.length || hold.scope === "task" ? "24 小时未回答会停下任务。" : "24 小时未回答，上限不变。"}{hold.reason === "review_budget" && "被拦下的动作不会自动重发。提高上限不会自动重发动作。"}</>}</p>
    {pending ? <div className="space-y-2" role="status">
      <p className="text-sm">{pending.impact}</p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" disabled={busy} onClick={() => void confirm()}>{pendingDecision === "stop" ? "确认停下" : unanswered ? "确认继续" : "确认提高上限"}</Button>
        <Button type="button" variant="outline" disabled={busy} onClick={() => setPending(null)}>取消</Button>
      </div>
    </div> : <div className="flex flex-wrap items-end gap-2">
      {!unanswered && <label className="space-y-1 text-sm pointer-coarse:text-base">新上限（美元）
        <Input className="w-40 pointer-coarse:text-base" aria-label={`C-${hold.id} 新上限（美元）`} inputMode="decimal" value={amount}
          onChange={(event) => setAmount(event.target.value)} placeholder="继续时可提高" disabled={busy} />
      </label>}
      <Button type="button" className="pointer-coarse:text-base" disabled={busy} onClick={() => void decide("continue")}>继续</Button>
      <Button type="button" className="pointer-coarse:text-base" variant="outline" disabled={busy} onClick={() => void decide("stop")}>停</Button>
    </div>}
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
  </li>;
}

function callDecide(holdId: number, decision: "continue" | "stop", value: number | undefined) {
  return callV1<Record<string, unknown>>("/api/v1/cost_holds/decide", {
    hold_id: holdId, decision, ...(decision === "continue" && value !== undefined ? { new_limit_usd: value } : {}),
  });
}

function updateHold(pending: V1PendingConfirmation) {
  return confirmV1<Record<string, unknown>>(pending.confirmation_id, pending.session);
}

/** Settings → 花费: edit admission caps, then read the stored view back. Opening the page does not write. */
export function CostGuardSettingsSection() {
  const [view, setView] = useState<CostGuardView | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [writeError, setWriteError] = useState("");
  const [stale, setStale] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [pending, setPending] = useState<V1PendingConfirmation | null>(null);
  const [busy, setBusy] = useState(false);
  const [holds, setHolds] = useState<CostHold[] | null>(null);
  const [holdsError, setHoldsError] = useState("");
  const [holdsLoading, setHoldsLoading] = useState(true);
  const active = useRef(true);
  const request = useRef<AbortController | null>(null);
  const draftRef = useRef<Draft | null>(null);
  const saveRef = useRef<HTMLButtonElement>(null);
  const returnFocus = useRef(false);
  const busyRef = useRef(false);
  draftRef.current = draft;

  async function loadView(signal: AbortSignal, replaceDraft: boolean) {
    try {
      const next = await fetchCostGuard(signal);
      if (!active.current || signal.aborted) return;
      setView(next);
      if (replaceDraft || !draftRef.current) setDraft(draftFrom(next));
      setLoadError(null); setStale(false); setUnknown(false); setForbidden(false);
    } catch (error) {
      if (!active.current || signal.aborted || stopped(error)) return;
      if (error instanceof V1Error && error.status === 403) {
        setForbidden(true);
        setLoadError("仅操作者可查看和修改花费设置");
        if (replaceDraft) { setView(null); setDraft(null); }
        return;
      }
      setLoadError(messageOf(error));
      setStale(true);
    } finally {
      if (active.current && !signal.aborted) setLoading(false);
    }
  }

  async function loadHolds(signal?: AbortSignal) {
    try {
      const next = await fetchCostHolds(signal);
      if (!active.current || signal?.aborted) return;
      setHolds(next.holds); setHoldsError("");
    } catch (error) {
      if (!active.current || signal?.aborted || stopped(error)) return;
      setHoldsError(messageOf(error));
    } finally {
      if (active.current && !signal?.aborted) setHoldsLoading(false);
    }
  }

  function refresh(replaceDraft: boolean) {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    void loadView(controller.signal, replaceDraft);
    void loadHolds(controller.signal);
  }

  useEffect(() => {
    active.current = true;
    refresh(true);
    return () => { active.current = false; request.current?.abort(); };
    // The first read is the only automatic one. Later reads are explicit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (returnFocus.current && !pending && !unknown) {
      saveRef.current?.focus();
      returnFocus.current = false;
    }
  }, [pending, unknown, view]);

  const parsed = view && draft ? changedPatch(view, draft) : { valid: true, dirty: false, patch: null };
  const locked = Boolean(pending) || forbidden || busy;
  const canSave = Boolean(parsed.patch && parsed.valid && parsed.dirty && !locked && !unknown && !loading);

  async function readBack() {
    try {
      const next = await fetchCostGuard();
      if (!active.current) return;
      setView(next); setDraft(draftFrom(next));
      setUnknown(false); setWriteError(""); setLoadError(null); setStale(false);
      await loadHolds();
    } catch (error) {
      if (!active.current || stopped(error)) return;
      setUnknown(true);
      setWriteError("状态未知。请只读重试，不会自动再次保存。");
    }
  }

  async function save() {
    if (!canSave || !parsed.patch || busyRef.current) return;
    busyRef.current = true; setBusy(true); setWriteError("");
    try {
      const result = await updateCostGuard(parsed.patch);
      if (!active.current) return;
      if (isPending(result)) { setPending(result); return; }
      await readBack();
      returnFocus.current = true;
    } catch (error) {
      if (!active.current || stopped(error)) return;
      if (error instanceof V1Error && error.status === 403) setForbidden(true);
      setWriteError(messageOf(error));
    } finally {
      busyRef.current = false;
      if (active.current) setBusy(false);
    }
  }

  async function confirmSave() {
    if (!pending || busyRef.current) return;
    busyRef.current = true; setBusy(true); setWriteError("");
    try {
      const result = await updateCostGuard(parsed.patch ?? {}, pending);
      if (!active.current) return;
      if (isPending(result)) { setPending(result); return; }
      setPending(null);
      await readBack();
      returnFocus.current = true;
    } catch (error) {
      if (!active.current || stopped(error)) return;
      setPending(null);
      if (error instanceof V1Error && error.status === 403) setForbidden(true);
      setWriteError(messageOf(error));
    } finally {
      busyRef.current = false;
      if (active.current) setBusy(false);
    }
  }

  function edit(next: Draft) {
    if (pending || forbidden) return;
    setDraft(next); setWriteError("");
  }

  const showForm = Boolean(view && draft && !(forbidden && !view));
  return <div className="min-w-0 space-y-6">
    {loading && !view && <p role="status" className="text-sm text-muted-foreground">正在读取花费设置…</p>}
    {loadError && <p role="alert" className="text-sm text-destructive">{loadError}</p>}
    {unknown && <p role="alert" className="text-sm text-destructive">状态未知。请只读重试，不会自动再次保存。</p>}
    {stale && view && <p role="status" className="text-sm text-warning">数据未刷新</p>}
    {(loadError || unknown) && !forbidden && <Button type="button" variant="outline" className="pointer-coarse:text-base" onClick={() => refresh(false)}>重新读取</Button>}
    {showForm && view && draft && <Card>
      <CardHeader><CardTitle className="flex flex-wrap items-center gap-3">花费守卫 <Badge variant="outline">{modeLabel(view.mode)}</Badge></CardTitle></CardHeader>
      <CardContent className="space-y-4 text-sm">
        {view.problem && <p role="alert" className="text-destructive">{view.problem}</p>}
        <p>{view.mode === "enforce"
          ? "任务按已花和预留检查上限；选择询问时等待你决定，全局当天上限会暂停新的任务运行。范围链设有上限时，订阅运行会按预留金额事中软停；没有上限则不软停。fail 会计入预留，可能提前停止任务及正在运行的 Worker。收尾回合也按实际准入结果决定。金额上限拒绝优先于用量窗口暂缓。账号被拒或配额失败时，包括你自己发起的运行都会暂缓到重置；重置时间未知时最多冷却五分钟。95% 利用率只暂缓后台角色和定时触发。操作者的管家回合可越过金额上限，但仍受被拒窗口约束；不自动换账号。后台角色达到自身、定时任务或全局当天上限时停止；定时触发到上限时跳过这次。"
          : "先观察一周，再根据实际花费决定上限。目前只记录预留和本来会被拦下的运行，不拦截、不提问、不发消息。"}</p>
        <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label className={FIELD}>模式
            <select id="cost-guard-mode" aria-label="模式" className={SELECT} value={draft.mode} disabled={locked}
              onChange={(event) => edit({ ...draft, mode: event.target.value as CostGuardMode })}>
              <option value="off">关闭</option>
              <option value="observe">仅观察</option>
              <option value="enforce">执行中</option>
            </select>
          </label>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <label className={FIELD}>全局每日真钱上限
              <Input id="cost-guard-daily-usd" aria-label="全局每日真钱上限" className="pointer-coarse:text-base" inputMode="decimal"
                placeholder="未设上限" value={draft.daily_usd} disabled={locked}
                onChange={(event) => edit({ ...draft, daily_usd: event.target.value })} />
            </label>
            <label className={FIELD}>全局每日等价花费上限
              <Input id="cost-guard-daily-equivalent-usd" aria-label="全局每日等价花费上限" className="pointer-coarse:text-base" inputMode="decimal"
                placeholder="未设上限" value={draft.daily_equivalent_usd} disabled={locked}
                onChange={(event) => edit({ ...draft, daily_equivalent_usd: event.target.value })} />
            </label>
          </div>
          <p className="text-muted-foreground">留空表示未设上限。建议值不会自动填入。</p>
          <ul className="space-y-4">
            {COST_GUARD_ROLES.map((role) => <li key={role} className="grid grid-cols-1 gap-3 border-b border-border pb-4 last:border-0 md:grid-cols-[minmax(9rem,12rem)_1fr_1fr] md:items-end">
              <p className="font-medium">{role}</p>
              <label className={FIELD}>每日真钱
                <Input id={`cost-guard-${role}-daily-usd`} aria-label={`${role} 每日真钱`} className="pointer-coarse:text-base" inputMode="decimal"
                  placeholder="未设上限" value={draft.roles[role].daily_usd} disabled={locked}
                  onChange={(event) => edit({ ...draft, roles: { ...draft.roles, [role]: { ...draft.roles[role], daily_usd: event.target.value } } })} />
              </label>
              <label className={FIELD}>每日等价花费
                <Input id={`cost-guard-${role}-daily-equivalent-usd`} aria-label={`${role} 每日等价花费`} className="pointer-coarse:text-base" inputMode="decimal"
                  placeholder="未设上限" value={draft.roles[role].daily_equivalent_usd} disabled={locked}
                  onChange={(event) => edit({ ...draft, roles: { ...draft.roles, [role]: { ...draft.roles[role], daily_equivalent_usd: event.target.value } } })} />
              </label>
            </li>)}
          </ul>
          {!parsed.valid && <p role="alert" className="text-destructive">请填写非负的有限金额。</p>}
          {writeError && <p role="alert" className="text-destructive">{writeError}</p>}
          {pending && <div role="status" className="space-y-2">
            <p>{pending.impact}</p>
            <div className="flex flex-wrap gap-2">
              <Button type="button" className="pointer-coarse:text-base" disabled={busy || forbidden} onClick={() => void confirmSave()}>确认保存</Button>
              <Button type="button" variant="outline" className="pointer-coarse:text-base" disabled={busy} onClick={() => setPending(null)}>取消确认</Button>
            </div>
          </div>}
          <div className="flex flex-wrap gap-2">
            <Button ref={saveRef} id="cost-guard-save" type="submit" className="pointer-coarse:text-base" aria-disabled={!canSave} disabled={locked || unknown || !parsed.valid}>保存</Button>
            <Button type="button" variant="outline" className="pointer-coarse:text-base" disabled={!parsed.dirty || locked} onClick={() => { setDraft(draftFrom(view)); setWriteError(""); }}>取消编辑</Button>
          </div>
        </form>
        <p className="text-muted-foreground">等价花费是订阅用量按 API 价格折算的数字，不是真钱。没有价格和单次上限的运行只在开始前估算，无法在运行中限额。</p>
        {(view.recommended_daily_usd !== null || view.recommended_daily_equivalent_usd !== null)
          ? <p>近 30 天每日花费 P95 × 1.5 建议值（须操作者确认）：真钱 {money(view.recommended_daily_usd)}；等价花费 {money(view.recommended_daily_equivalent_usd)}。建议不会自动生效。</p>
          : <p className="text-muted-foreground">观察满一周后才提供每日上限建议；建议不会自动生效。</p>}
      </CardContent>
    </Card>}
    {view?.holds && view.holds.length > 0 && <Card>
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
          <Button type="button" variant="outline" onClick={() => void loadHolds()}>重试</Button></div>
          : holdsLoading || holds === null ? <p role="status" className="text-sm text-muted-foreground">正在读取等待决定的花费…</p>
          : holds.length === 0 ? <p className="text-sm text-muted-foreground">没有等待决定的花费。</p>
          : <ul className="divide-y divide-border">{holds.map((hold) => <HoldDecision key={hold.id} hold={hold} onDecided={() => void loadHolds()} />)}</ul>}
      </CardContent>
    </Card>
    {view && <Card>
      <CardHeader><CardTitle>今天已花与预留</CardTitle></CardHeader>
      <CardContent>
        {view.counters.length === 0 ? <p className="text-sm text-muted-foreground">暂无计数；关闭时不收集守卫数据。</p>
          : <div className="max-w-full overflow-x-auto"><table className="w-full text-left text-sm">
            <thead><tr className="border-b border-border text-muted-foreground"><th className="pb-2 pr-4">范围</th><th className="pb-2 pr-4">币种</th><th className="pb-2 pr-4">已花</th><th className="pb-2 pr-4">预留</th><th className="pb-2">上限</th></tr></thead>
            <tbody>{view.counters.map((row) => <tr key={`${row.scope}:${row.scope_ref}:${row.currency}`} className="border-b border-border last:border-0">
              <td className="py-3 pr-4">{SCOPES[row.scope] ?? row.scope} {row.scope_ref}{row.cost_status === "unknown" ? " · 花费未知" : ""}</td>
              <td className="py-3 pr-4">{currency(row.currency)}</td>
              <td className="py-3 pr-4 font-mono">{money(row.spent_usd)}</td><td className="py-3 pr-4 font-mono">{money(row.reserved_usd)}</td><td className="py-3 font-mono">{money(row.limit_usd)}</td>
            </tr>)}</tbody>
          </table></div>}
      </CardContent>
    </Card>}
    {view && <Card>
      <CardHeader><CardTitle>运行观察（最近 20 条）</CardTitle></CardHeader>
      <CardContent>
        {view.recent.length === 0 ? <p className="text-sm text-muted-foreground">暂无观察记录。</p>
          : <ul className="divide-y divide-border text-sm">{view.recent.slice(0, 20).map((row, index) => <li key={`${row.at}:${index}`} className="space-y-1 py-3 first:pt-0 last:pb-0">
            <p>{[row.run_kind, row.currency ? `${currency(row.currency)} ${typeof row.estimate_usd === "number" ? money(row.estimate_usd) : ""}`.trim() : null, outcomeText(row)].filter(Boolean).join(" · ")}</p>
            <p className="text-xs text-muted-foreground">{SCOPES[row.scope ?? ""] ?? row.scope} {row.scope_ref} · {SOURCES[row.estimate_source] ?? row.estimate_source}{row.at ? ` · ${new Date(row.at).toLocaleString()}` : ""}</p>
          </li>)}</ul>}
      </CardContent>
    </Card>}
  </div>;
}
