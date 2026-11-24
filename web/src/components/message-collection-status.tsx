import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { getMessageCollection, type MessageCollection } from "@/lib/api-message-collection";
import { startPolling } from "@/lib/polling";
import { errorText } from "@/lib/use-action";

const AUTH: Record<string, string> = { ready: "登录可用", needs_refresh: "下次读取时刷新", expired: "登录已过期", missing: "缺少登录" };
const PARITY = { pending: "等待采集", matched: "一致", drift: "存在差异", incomplete: "证据不足" };
const REASON: Record<string, string> = { reference_changed: "参考文件已变化", reference_invalid: "参考不可用",
  coverage_missing: "窗口尚未完整读取", collection_failed: "采集失败", policy_changed: "采集范围已变化",
  evidence_deleted: "证据已按删除请求移除", identity_conflict: "消息身份冲突" };
const SEND: Record<string, string> = { pending: "待投递", sending: "投递中", sent: "已投递", rejected: "投递被拒绝",
  unknown: "投递结果未知，请核对聊天", dashboard: "仅在面板可见", deferred: "已推迟，待摘要或人工处理" };
const ALERT: Record<string, string> = { parity_drift: "对账差异", collector_failure: "采集失败", stale: "一小时未成功采集",
  auth: "登录需处理", auth_expiring: "登录即将到期", partial: "连续三轮未完成" };
const number = (value: number | null | undefined) => value == null ? "未知" : value.toLocaleString();
const date = (value: string | null) => value ? new Date(value).toLocaleString() : "尚无记录";

/** Mounted only for collect sources or an explicit Attention deep link. It never changes the source's mode. */
export function MessageCollectionStatus({ sourceId }: { sourceId: string }) {
  const [value, setValue] = useState<MessageCollection | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setValue(null); setError("");
    return startPolling(async (signal) => {
      try {
        const found = await getMessageCollection(sourceId, signal);
        if (!signal.aborted) { setValue(found); setError(""); }
      } catch (err) { if (!signal.aborted) setError(errorText(err)); }
    }, 15_000);
  }, [sourceId]);
  const parity = value?.parity;
  const run = value?.latest_run;
  return <Card>
    <CardHeader><CardTitle className="text-base text-foreground">消息流采集与对账</CardTitle></CardHeader>
    <CardContent className="space-y-4 text-sm [overflow-wrap:anywhere]">
      {error && <p role="alert" className="text-destructive">无法读取采集状态：{error}</p>}
      {!value && !error && <p role="status" className="text-muted-foreground">正在读取采集状态…</p>}
      {value && <>
        <p className="text-muted-foreground">当前模式：{value.ingest_mode}。采集规则：<span className="font-mono">{value.settings.collect_cron}</span>。</p>
        <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div><dt className="text-xs text-muted-foreground">登录</dt><dd>{!value.auth.checked_at && !["expired", "missing"].includes(value.auth.state ?? "") ? "未验证" : AUTH[value.auth.state ?? ""] ?? "未验证"}</dd>
            <dd className="text-xs text-muted-foreground">{value.auth.refresh_expires_at ? `到期：${date(value.auth.refresh_expires_at)}` : "到期时间未知"}</dd></div>
          <div><dt className="text-xs text-muted-foreground">上次完整采集</dt><dd>{date(value.last_collect_ok_at)}</dd></div>
          <div><dt className="text-xs text-muted-foreground">待重试</dt><dd>会话 {number(value.debt.chats)} · 话题 {number(value.debt.threads)}</dd></div>
          <div><dt className="text-xs text-muted-foreground">最近运行</dt><dd>{run?.status ?? "尚无运行"}</dd></div>
        </dl>
        {run && <p>搜索命中 {number(run.search_hits)} · 搜索未索引 {number(run.unindexed)} · 兜底 {number(run.sweep_chats)} 个会话、发现 {number(run.sweep_found)} 条 · 新增 {number(run.events_new)} · 重复 {number(run.events_seen)}</p>}
        {value.fallback_24h && <p className={value.fallback_24h.ratio != null && value.fallback_24h.ratio > 0.02 ? "text-warning" : "text-muted-foreground"}>
          24 小时兜底发现新 ID {number(value.fallback_24h.sweep_found)} / 新 ID {number(value.fallback_24h.events_new)} · 比率 {value.fallback_24h.ratio == null ? "未知" : `${(value.fallback_24h.ratio * 100).toFixed(1)}%`}
          {value.fallback_24h.ratio != null && value.fallback_24h.ratio > 0.02 ? "，超过 2%，需要检查搜索覆盖。" : ""}
        </p>}
        <section aria-label="采集对账" className="space-y-2 border-t border-border pt-3">
          <h3 className="font-medium">24 小时冻结窗口对账</h3>
          {!parity ? <p className="text-muted-foreground">没有冻结参考，尚不能判断采集是否一致。</p> : <>
            <p className="flex flex-wrap items-center gap-2"><Badge variant={parity.state === "drift" ? "destructive" : parity.state === "matched" ? "success" : "warning"}>{PARITY[parity.state]}</Badge><span>会话 {parity.session}</span></p>
            <p className="text-xs text-muted-foreground">{date(parity.window.from)} → {date(parity.window.to)}</p>
            <p>覆盖完成 {number(parity.coverage.complete)} · 待完成 {number(parity.coverage.pending)} · 失败 {number(parity.coverage.failed)}</p>
            <p>参考 {number(parity.totals.bridge)} · 实际观察 {number(parity.totals.collect)} · 仅参考 {number(parity.totals.bridge_only)} · 仅采集 {number(parity.totals.collect_only)}</p>
            {parity.empty_window && <p className="text-muted-foreground">空窗口，尚不能证明采集有效。</p>}
            {parity.reason && <p>证据状态：{REASON[parity.reason] ?? "需要核对"}</p>}
            {parity.buckets?.some((bucket) => bucket.bridge_only || bucket.collect_only || bucket.identity_conflicts) &&
              <ul className="space-y-1 text-xs text-muted-foreground">{parity.buckets.filter((bucket) => bucket.bridge_only || bucket.collect_only || bucket.identity_conflicts).map((bucket) =>
                <li key={`${bucket.chat_id}:${bucket.hour}`}>{bucket.chat_id} · {new Date(bucket.hour).toISOString().slice(0, 13)}:00 UTC · 仅参考 {number(bucket.bridge_only)} · 仅采集 {number(bucket.collect_only)} · 身份冲突 {number(bucket.identity_conflicts)}</li>)}</ul>}
          </>}
        </section>
        <section aria-label="采集告警" className="space-y-2 border-t border-border pt-3">
          <h3 className="font-medium">告警与送达</h3>
          {value.alerts.length ? <ul className="space-y-2">{value.alerts.map((alert) => <li key={alert.id}>
            <span>{ALERT[alert.kind] ?? alert.kind} · {alert.resolved_at ? "已恢复" : "待处理"}</span>
            {alert.parity_session_id && <p className="text-xs text-muted-foreground">对账会话 {alert.parity_session_id}
              {typeof alert.metadata.window_from === "string" && typeof alert.metadata.window_to === "string"
                ? ` · ${date(alert.metadata.window_from)} → ${date(alert.metadata.window_to)}` : ""}
              {` · 仅参考 ${number(typeof alert.metadata.bridge_only === "number" ? alert.metadata.bridge_only : null)} · 仅采集 ${number(typeof alert.metadata.collect_only === "number" ? alert.metadata.collect_only : null)}`}
            </p>}
            <p className="text-xs text-muted-foreground">{SEND[alert.open_state] ?? alert.open_state}{alert.resolved_at ? `；恢复通知：${SEND[alert.recovery_state] ?? alert.recovery_state}` : ""}</p>
          </li>)}</ul> : <p className="text-muted-foreground">{value.settings.alerts_enabled ? "没有待处理的告警。" : "采集告警未启用。"}</p>}
        </section>
      </>}
    </CardContent>
  </Card>;
}
