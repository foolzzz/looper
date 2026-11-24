import { useEffect, useState } from "react";
import { fetchCostGuard, fetchCostHolds, type CostGuardView, type CostHold } from "@/lib/api-cost-guard";
import { startPolling } from "@/lib/polling";
import { SessionEndedError } from "@/lib/session";
import { V1Error } from "@/lib/v1";
import { cn } from "@/lib/utils";

const linkClass = "rounded-sm text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

function stopped(signal: AbortSignal, error: unknown): boolean {
  return signal.aborted || error instanceof SessionEndedError || (error instanceof DOMException && error.name === "AbortError");
}

function money(value: number): string {
  return `$${Math.abs(value).toFixed(2)}`;
}

/** Headroom for one global currency. Missing counters stay missing; other scopes are ignored. */
function currencyLine(view: CostGuardView, currency: "provider_priced_usd" | "equivalent_usd", label: string): string {
  const cap = currency === "provider_priced_usd" ? view.daily_usd : view.daily_equivalent_usd;
  const counter = view.counters.find((row) => row.scope === "global_day" && row.scope_ref === "global" && row.currency === currency);
  if (!counter) return `${label}：暂无守卫计数`;
  if (counter.cost_status !== "known") return `${label}：花费不完整/余量暂不可确定`;
  if (cap == null) return `${label}：未设上限`;
  const remain = cap - counter.spent_usd - counter.reserved_usd;
  const kind = view.mode === "observe" ? "观察余量" : "准入余量";
  if (remain < 0) return `${label}${kind}：超出 ${money(remain)}`;
  if (remain === 0) return `${label}${kind}：剩余 $0.00`;
  return `${label}${kind}：剩余 ${money(remain)}`;
}

function guardLines(view: CostGuardView, stale: boolean): string[] {
  return [
    currencyLine(view, "provider_priced_usd", "真钱"),
    currencyLine(view, "equivalent_usd", "等价花费"),
  ].map((line) => (stale ? `已过期 · ${line}` : line));
}

/** Global admission headroom and the pending-decision count. A failed read stays inside this block. */
export function CostGuardSummary() {
  const [view, setView] = useState<CostGuardView | null>(null);
  const [guardError, setGuardError] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [stale, setStale] = useState(false);
  const [guardSettled, setGuardSettled] = useState(false);
  const [holds, setHolds] = useState<CostHold[] | null>(null);
  const [holdsError, setHoldsError] = useState("");
  const [holdsSettled, setHoldsSettled] = useState(false);

  useEffect(() => startPolling(async (signal) => {
    await Promise.all([
      fetchCostGuard(signal).then(
        (next) => {
          if (signal.aborted) return;
          setView(next); setGuardError(""); setForbidden(false); setStale(false); setGuardSettled(true);
        },
        (error: unknown) => {
          if (stopped(signal, error)) return;
          setGuardSettled(true);
          if (error instanceof V1Error && error.status === 403) {
            setForbidden(true); setView(null); setStale(false); setGuardError(""); return;
          }
          setGuardError(error instanceof Error ? error.message : String(error));
          setStale(true);
        },
      ),
      fetchCostHolds(signal).then(
        (next) => { if (!signal.aborted) { setHolds(next.holds); setHoldsError(""); setHoldsSettled(true); } },
        (error: unknown) => { if (!stopped(signal, error)) { setHoldsSettled(true); setHoldsError("等待数量暂不可用"); } },
      ),
    ]);
  }, 5000), []);

  const showHeadroom = Boolean(view && !view.problem && view.mode !== "off");
  const lines = showHeadroom && view ? guardLines(view, stale) : [];
  const holdText = holds && holds.length === 0 ? "没有等待决定的花费" : holds ? `${holds.length} 条等待决定的花费` : "";
  return <div className="mt-3 space-y-1 text-sm" aria-label="花费守卫余量">
    {!guardSettled && !view && !guardError && !forbidden && <p role="status" className="text-muted-foreground">正在读取花费守卫…</p>}
    {forbidden && <p role="alert" className="text-destructive">仅操作者可查看花费余量</p>}
    {guardError && !view && <p role="alert" className="text-destructive">{guardError}</p>}
    {view && stale && <p role="status" className="text-warning">余量已过期</p>}
    {view?.mode === "off" && <p>{stale ? "已过期 · " : ""}守卫已关闭</p>}
    {view?.problem && <p role="alert" className="text-destructive">{stale ? "已过期 · " : ""}花费不完整/余量暂不可确定 {view.problem}</p>}
    {lines.map((line) => <p key={line} className={cn(stale && "text-warning")}>{line}</p>)}
    {guardError && view && <p role="alert" className="text-destructive">{guardError}</p>}
    {!holdsSettled && !holdsError && <p role="status" className="text-muted-foreground">正在读取等待决定的花费…</p>}
    {holdsError && <p role="alert" className="text-destructive">{holdsError}</p>}
    {holds && !holdsError && <a href="#settings/cost" className={linkClass}>{holdText}</a>}
  </div>;
}
