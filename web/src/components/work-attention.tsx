import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  attentionSettingsGet, fetchAttention, isAttentionOff, type AttentionItem, type AttentionKind, type AttentionSection,
  type AttentionSummary,
} from "@/lib/api-attention";
import { displayText as text, plainName } from "@/lib/attention-display";
import { sinceText } from "@/lib/gates";
import { startPolling } from "@/lib/polling";
import { errorText } from "@/lib/use-action";
import { isMissingOperation } from "@/lib/work";

/** How often the manager column reads the summary again while it is visible. */
const REFRESH_MS = 15_000;
const channelLabel = (channel: string) => ({ web: "网页对话", lark: "飞书", lark_bot: "飞书（机器人）", telegram: "Telegram" } as Record<string, string>)[channel] ?? channel;

const LINK = "rounded-sm font-medium underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring pointer-coarse:inline-flex pointer-coarse:min-h-11 pointer-coarse:items-center";

const TITLES: Record<AttentionKind, string> = {
  failure_holds: "运行失败等你处理",
  gates: "等待你回答的关卡",
  confirmations: "待确认的操作",
  schedules: "失败的定时任务",
  logins: "登录异常",
  intents: "意图",
  ledger: "待处理的账本事项",
  deliveries: "可能未送达的消息",
  reminders: "未确认送达的提醒",
  cost: "花费等你决定",
  vault_commits: "知识库提交受阻",
  task_manager_refs: "任务管家改了别的分支或标签",
};

const ALERTS: Record<string, string> = {
  failures: "失败次数告警", stale: "长时间未成功", deadline: "已超过截止时间", overlap: "运行重叠",
  unanswered: "无人回复，已自动暂停",
};
const LEDGER_KINDS: Record<string, string> = { pending: "待办", decision: "待确认的决定", work_item: "工作事项" };
const DELIVERY_SOURCES: Record<string, string> = {
  subscription: "订阅推送", intent: "意图消息", task_end: "任务结束通知",
  catchup: "飞书补拉丢失",
  message_stream: "消息流采集/对账需处理",
};

const SECRET_KEY = /token|secret|password|credential|authorization|cookie|api[_-]?key|private[_-]?key/i;
const count = (value: unknown) => (typeof value === "number" ? value : 0);

/** Facts the operator can read. Secret keys never reach the line. */
function visibleFacts(facts: Record<string, unknown>): Record<string, unknown> {
  const copy: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(facts)) {
    if (!SECRET_KEY.test(key)) copy[key] = value;
  }
  return copy;
}

/** The second line of an item: where it belongs and the kind's own facts, in words. */
function details(kind: AttentionKind, item: AttentionItem): { line: string; badges: string[] } {
  const facts = visibleFacts(item.facts);
  const context = text(item.context);
  const ref = text(item.ref);
  const parts = (...values: (string | null | false | undefined)[]) => values.filter(Boolean).join(" · ");
  switch (kind) {
    case "failure_holds":
      return { line: parts(context, `连续失败 ${count(facts.streak)} 次`), badges: [text(facts.kind)] };
    case "gates":
      return { line: parts(context, text(facts.step),
        count(facts.questions) > 0 && `${count(facts.questions)} 个未回答的问题`), badges: [] };
    case "confirmations": {
      const minutes = Math.max(1, Math.floor(count(facts.expires_in_seconds) / 60));
      return { line: parts(context, `来自 ${channelLabel(text(facts.channel)) || "对话"}`, `${minutes} 分钟后过期`),
        badges: [] };
    }
    case "schedules": {
      const failures = count(facts.failures);
      const alerts = Array.isArray(facts.alerts)
        ? facts.alerts.map((alert) => text(ALERTS[String(alert)] ?? String(alert))) : [];
      return { line: context, badges: [...(failures > 0 ? [`连续失败 ${failures} 次`] : []), ...alerts] };
    }
    case "logins":
      return { line: parts(text(facts.detail), text(facts.fix) && `处理方法：${text(facts.fix)}`), badges: [] };
    case "intents":
      return { line: parts(ref, context), badges: facts.priority === "urgent" ? ["紧急"] : [] };
    case "ledger":
      return { line: parts(ref, LEDGER_KINDS[text(facts.kind)] ?? text(facts.kind)),
        badges: facts.overdue === true ? ["已逾期"] : [] };
    case "reminders":
      return { line: parts(ref, context), badges: [] };
    case "cost":
      if (facts.reason === "unanswered") {
        return { line: parts(ref, context, count(facts.pushes) > 0 && `${count(facts.pushes)} 条推送没人回`), badges: [] };
      }
      return { line: parts(ref, context, facts.currency === "equivalent_usd" ? "等价花费" : "真钱",
        `已花及预留 $${count(facts.spent_usd).toFixed(2)} / 上限 $${count(facts.limit_usd).toFixed(2)}`), badges: [] };
    case "deliveries":
      if (facts.source === "message_stream")
        return { line: parts(DELIVERY_SOURCES.message_stream, context,
          text(facts.session) && `对账 ${text(facts.session)}`,
          facts.kind === "parity_drift" && `仅参考 ${count(facts.bridge_only)} · 仅采集 ${count(facts.collect_only)}`,
          text(facts.fix)), badges: [] };
      return { line: parts(DELIVERY_SOURCES[text(facts.source)] ?? text(facts.source), ref,
        text(facts.channel) && `${facts.source === "catchup" ? "来自" : "发往"} ${channelLabel(text(facts.channel))}`,
        text(facts.fix)), badges: [] };
    case "vault_commits":
      return { line: parts(context, text(facts.reason), text(facts.branch), text(facts.worktree)), badges: ["受阻"] };
    case "task_manager_refs":
      if (["ref_symlink", "ref_hardlink", "ref_symref_target", "ref_unchecked"].includes(text(facts.reason)))
        return { line: parts(context, "删掉或换成普通文件后再发消息",
          Array.isArray(facts.refs) && facts.refs.map((name) => plainName(name)).filter(Boolean).join("、")), badges: [] };
      return { line: parts(context, `${count(facts.count)} 个分支或标签被改`,
        Array.isArray(facts.refs) && facts.refs.map((name) => plainName(name)).filter(Boolean).slice(0, 5).join("、")), badges: [] };
  }
}

/** Stay inside Work only for the exact `#tasks/<id>` form. Anything else keeps the server href. */
function workHref(href: string): string {
  const match = /^#tasks\/([^/?#]+)$/.exec(href);
  return match ? `#work/task/${match[1]}` : href;
}

type Phase = "loading" | "ready" | "off" | "unsupported" | "error";

function Item({ kind, item }: { kind: AttentionKind; item: AttentionItem }) {
  const { line, badges } = details(kind, item);
  const href = item.href ? workHref(item.href) : null;
  const title = text(item.title);
  const context = text(item.context);
  const extra = context && !line.includes(context) ? context : "";
  const note = [extra, line, ...badges].filter(Boolean).join(" · ");
  return (
    <li className="space-y-0.5 py-1.5 text-sm">
      {href ? <a href={href} className={LINK}>{title}</a> : <span className="font-medium [overflow-wrap:anywhere]">{title}</span>}
      {note && <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{note}</p>}
      {item.since && (
        <time dateTime={item.since} className="block text-xs tabular-nums text-muted-foreground">{sinceText(item.since, "zh-CN")}</time>
      )}
    </li>
  );
}

function SectionView({ section, onRetry }: { section: AttentionSection; onRetry: () => void }) {
  const more = section.count - section.items.length;
  return (
    <section aria-label={TITLES[section.kind]} className="space-y-1">
      <h3 className="text-xs font-semibold">{TITLES[section.kind]}</h3>
      {section.state === "error" ? (
        <div role="alert" className="space-y-2 text-sm text-destructive [overflow-wrap:anywhere]">
          <p>{text(section.error) || "此来源读取失败"}</p>
          <Button type="button" variant="outline" size="sm" onClick={onRetry}>重试</Button>
        </div>
      ) : section.state === "unknown" ? (
        <p role="status" className="text-sm text-muted-foreground">此来源状态未知</p>
      ) : section.count === 0 ? (
        <p className="text-sm text-muted-foreground">没有待办</p>
      ) : (
        <ul>
          {section.items.map((entry) => <Item key={entry.key} kind={section.kind} item={entry} />)}
          {more > 0 && <li className="text-xs text-muted-foreground">还有 {more} 项</li>}
        </ul>
      )}
    </section>
  );
}

/** Read-only needs-you summary for the manager column. One poll while mounted; unmount aborts it. */
export function WorkAttention() {
  const [phase, setPhase] = useState<Phase>("loading");
  const [summary, setSummary] = useState<AttentionSummary | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const latest = useRef(0);
  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  const read = useCallback(async (signal: AbortSignal) => {
    const seq = ++latest.current;
    try {
      const settings = await attentionSettingsGet(signal);
      if (signal.aborted || seq !== latest.current) return;
      if (!settings.enabled) {
        setPhase("off");
        setSummary(null);
        setError("");
        return;
      }
      const next = await fetchAttention(signal);
      if (signal.aborted || seq !== latest.current) return;
      setSummary(next);
      setError("");
      setPhase("ready");
    } catch (err) {
      if (signal.aborted || seq !== latest.current || (err instanceof Error && err.name === "AbortError")) return;
      if (isAttentionOff(err)) {
        setPhase("off");
        setSummary(null);
        setError("");
        return;
      }
      if (isMissingOperation(err)) {
        setPhase("unsupported");
        setSummary(null);
        setError("");
        return true;
      }
      setPhase("error");
      setError(errorText(err));
    }
  }, []);

  useEffect(() => startPolling((signal) => read(signal), REFRESH_MS), [attempt, read]);

  const sections = summary?.sections ?? [];
  const nothing = phase === "ready" && sections.length > 0 && summary?.total === 0
    && sections.every((section) => section.state === "ok" && section.count === 0);

  return (
    <aside aria-label="需要你处理" className="flex min-h-0 w-full flex-1 flex-col overflow-y-auto border-t border-border xl:w-80 xl:flex-none xl:shrink-0 xl:border-l xl:border-t-0">
      <div className="space-y-3 p-4">
        <h2 className="text-sm font-semibold">需要你处理</h2>
        {phase === "loading" && !summary && (
          <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            正在读取待办…
          </p>
        )}
        {phase === "off" && <p role="status" className="text-sm text-muted-foreground">待办汇总未开启</p>}
        {phase === "unsupported" && <p role="status" className="text-sm text-muted-foreground">此服务器尚不支持待办汇总</p>}
        {phase === "error" && (
          <div role="alert" className="space-y-2 text-sm text-destructive [overflow-wrap:anywhere]">
            <p>待办读取失败</p>
            {error && <p>{text(error)}</p>}
            <Button type="button" variant="outline" size="sm" onClick={retry}>重试</Button>
          </div>
        )}
        {nothing && <p role="status" className="text-sm text-muted-foreground">暂时没有需要你处理的事</p>}
        {phase !== "off" && phase !== "unsupported" && sections.map((section) => (
          <SectionView key={section.kind} section={section} onRetry={retry} />
        ))}
      </div>
    </aside>
  );
}
