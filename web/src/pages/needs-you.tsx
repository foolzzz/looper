import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  attentionSettingsGet, fetchAttention, isAttentionOff, type AttentionItem, type AttentionKind, type AttentionSection,
  type AttentionSummary,
} from "@/lib/api-attention";
import { channelLabel } from "@/lib/chat-channels";
import { sinceText } from "@/lib/gates";
import { startPolling } from "@/lib/polling";
import { errorText } from "@/lib/use-action";
import { cn } from "@/lib/utils";

/** How often the page reads the summary again. */
const REFRESH_MS = 15_000;

const linkClass =
  "rounded-sm hover:underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const TITLES: Record<AttentionKind, string> = {
  gates: "Gates waiting for your answer",
  confirmations: "Operations to confirm",
  schedules: "Failing schedules",
  logins: "Logins that fail",
  intents: "Intents 意图",
  ledger: "Ledger items waiting for you",
  deliveries: "Messages that may not have arrived",
  cost: "花费等你决定",
};

/** What to do about a section, said once under its heading. */
const HOW: Partial<Record<AttentionKind, string>> = {
  cost: "在花费设置中提高上限并继续，或停下；也可在私聊回复「继续」或「停」。",
  gates: "Answer on the task's page, or with /gates in your chat.",
  confirmations: "Reply 确认 in the chat that asked, before it lapses; anything else drops it.",
  intents: "Build or reject each one on the Intents page, or reply 建 / 驳回 with its number in your chat.",
  ledger: "Confirm, close or amend them through the Manager, or with looper ledger on this machine.",
  deliveries: "Looper never sends these again: check the chat, and resend by hand if it is missing.",
};

const ALERTS: Record<string, string> = {
  failures: "failures alert", stale: "no success for too long", deadline: "missed its deadline", overlap: "runs overlap",
};
const LEDGER_KINDS: Record<string, string> = { pending: "to-do", decision: "decision to confirm", work_item: "work item" };
const DELIVERY_SOURCES: Record<string, string> = {
  subscription: "Subscription push", intent: "Intent message", task_end: "Task end notice",
};

const text = (value: unknown) => (typeof value === "string" ? value : "");
const count = (value: unknown) => (typeof value === "number" ? value : 0);
const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** The second line of an item: what it belongs to and the kind's own facts, in words. */
function details(kind: AttentionKind, item: AttentionItem): { line: string; badges: string[] } {
  const { facts, context, ref } = item;
  const parts = (...values: (string | null | false | undefined)[]) => values.filter(Boolean).join(" · ");
  switch (kind) {
    case "gates":
      return { line: parts(context, text(facts.step),
        count(facts.questions) > 0 && plural(count(facts.questions), "open question")), badges: [] };
    case "confirmations": {
      const minutes = Math.max(1, Math.floor(count(facts.expires_in_seconds) / 60));
      return { line: parts(context, `in ${channelLabel(text(facts.channel) || "a chat")}`, `lapses in ${minutes} min`),
        badges: [] };
    }
    case "schedules": {
      const failures = count(facts.failures);
      const alerts = Array.isArray(facts.alerts) ? facts.alerts.map((a) => ALERTS[String(a)] ?? String(a)) : [];
      return { line: context, badges: [...(failures > 0 ? [`${plural(failures, "failure")} in a row`] : []), ...alerts] };
    }
    case "logins":
      return { line: parts(text(facts.detail), text(facts.fix) && `To fix: ${text(facts.fix)}`), badges: [] };
    case "intents":
      return { line: parts(ref, context), badges: facts.priority === "urgent" ? ["urgent"] : [] };
    case "ledger":
      return { line: parts(ref, LEDGER_KINDS[text(facts.kind)] ?? text(facts.kind)),
        badges: facts.overdue === true ? ["overdue"] : [] };
    case "cost":
      return { line: parts(ref, context, facts.currency === "equivalent_usd" ? "等价花费" : "真钱",
        `已花及预留 $${count(facts.spent_usd).toFixed(2)} / 上限 $${count(facts.limit_usd).toFixed(2)}`), badges: [] };
    case "deliveries":
      return { line: parts(DELIVERY_SOURCES[text(facts.source)] ?? text(facts.source), ref,
        text(facts.channel) && `to ${channelLabel(text(facts.channel))}`), badges: [] };
  }
}

function Item({ kind, item }: { kind: AttentionKind; item: AttentionItem }) {
  const { line, badges } = details(kind, item);
  return (
    <li className="flex flex-col gap-1 py-2.5 text-sm sm:flex-row sm:items-baseline sm:gap-4">
      <div className="min-w-0 flex-1 space-y-0.5">
        {item.href ? (
          <a href={item.href} className={cn(
            "font-medium [overflow-wrap:anywhere] pointer-coarse:inline-flex pointer-coarse:min-h-11 pointer-coarse:items-center",
            linkClass)}>{item.title}</a>
        ) : (
          <span className="font-medium [overflow-wrap:anywhere]">{item.title}</span>
        )}
        {(line || badges.length > 0) && (
          <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            {line && <span className="min-w-0 [overflow-wrap:anywhere]">{line}</span>}
            {badges.map((badge) => (
              <Badge key={badge} variant="warning" className="gap-1">
                <AlertTriangle className="h-3 w-3" aria-hidden="true" />{badge}
              </Badge>
            ))}
          </div>
        )}
      </div>
      {item.since && (
        <time dateTime={item.since} title={new Date(item.since).toLocaleString()}
          className="shrink-0 text-xs tabular-nums text-muted-foreground">
          {sinceText(item.since)} ago
        </time>
      )}
    </li>
  );
}

function Section({ section }: { section: AttentionSection }) {
  const { kind, state, items } = section;
  const more = section.count - items.length;
  return (
    <section aria-label={TITLES[kind]} className="rounded-lg border border-border bg-card px-5 py-4">
      <h2 className="flex items-baseline gap-2 text-sm font-semibold">
        {TITLES[kind]}
        {state === "ok" && <span className="tabular-nums text-muted-foreground">{section.count}</span>}
      </h2>
      {state === "error" ? (
        <p role="alert" className="mt-2 flex gap-2 text-sm text-destructive [overflow-wrap:anywhere]">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          Could not read this: {section.error}
        </p>
      ) : (
        <>
          {HOW[kind] && <p className="mt-1 text-xs text-muted-foreground">{HOW[kind]}</p>}
          <ul className="mt-1 divide-y divide-border">
            {items.map((item) => <Item key={item.key} kind={kind} item={item} />)}
          </ul>
          {more > 0 && <p className="mt-1 text-xs text-muted-foreground">and {more} more</p>}
        </>
      )}
    </section>
  );
}

/** `#todo`: everything that waits for the operator, from `attention.summary`, while the setting is on. */
export function NeedsYouPage() {
  const [summary, setSummary] = useState<AttentionSummary | null>(null);
  const [error, setError] = useState("");
  const [off, setOff] = useState(false);
  const latest = useRef(0);

  const read = useCallback(async (signal?: AbortSignal) => {
    const seq = ++latest.current;
    try {
      const settings = await attentionSettingsGet();
      if (seq !== latest.current || signal?.aborted) return;
      if (!settings.enabled) {
        setSummary(null); setError(""); setOff(true);
        return;
      }
      const next = await fetchAttention(signal);
      if (seq !== latest.current || signal?.aborted) return;
      setSummary(next); setError(""); setOff(false);
    } catch (err) {
      if (seq !== latest.current || signal?.aborted) return;
      // The setting can also turn off between these two reads.
      if (isAttentionOff(err)) { setSummary(null); setError(""); setOff(true); }
      else setError(errorText(err));
    }
  }, []);

  useEffect(() => startPolling((signal) => read(signal), REFRESH_MS), [read]);

  // Failing logins that were read are left to the banner every page shows (ClaudeCheckBanner reads the same
  // checks); /todo lists them. A logins section that could not be read stays.
  const sections = summary?.sections.filter((s) => s.kind !== "logins" || s.state !== "ok") ?? [];
  const shown = sections.filter((s) => s.state === "error" || (s.state === "ok" && s.count > 0));
  const clear = sections.filter((s) => s.state === "ok" && s.count === 0);
  const unknown = sections.filter((s) => s.state === "unknown");
  const total = shown.reduce((sum, s) => sum + (s.state === "ok" ? s.count : 0), 0);
  // "Nothing waits" only when every source was read; otherwise the ones not read are named below.
  const unread = sections.filter((s) => s.state !== "ok").length;
  const headline = total > 0 ? `${plural(total, "thing")} ${total === 1 ? "waits" : "wait"} for you`
    : unread > 0 ? `Nothing found in what could be read; ${plural(unread, "source")} could not be read`
    : "Nothing waits for you";

  return (
    <div className="mx-auto max-w-4xl space-y-4">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold">Needs you</h1>
        {summary && (
          <p className="text-sm text-muted-foreground">
            {headline}
            {" · read "}
            <time dateTime={summary.read_at}>{new Date(summary.read_at).toLocaleTimeString()}</time>
          </p>
        )}
      </div>

      {off && (
        <p role="status" className="text-sm text-muted-foreground">
          This summary is off. Set the system setting <code className="font-mono text-xs">attention.enabled</code> to
          {" "}<code className="font-mono text-xs">true</code> to use it.
        </p>
      )}
      {error && (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive [overflow-wrap:anywhere]">
          <span>Cannot load what needs you ({error}){summary && "; this is the last summary read."}</span>
          <Button type="button" variant="outline" size="sm" className="min-h-11" onClick={() => void read()}>
            Retry
          </Button>
        </div>
      )}
      {!summary && !error && !off && (
        <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Loading…
        </p>
      )}

      {shown.map((section) => <Section key={section.kind} section={section} />)}

      {summary && (clear.length > 0 || unknown.length > 0) && (
        <div className="space-y-1 text-xs text-muted-foreground">
          {clear.length > 0 && <p>Nothing in: {clear.map((s) => TITLES[s.kind]).join(", ")}.</p>}
          {unknown.length > 0 && <p>Not known on this server: {unknown.map((s) => TITLES[s.kind]).join(", ")}.</p>}
        </div>
      )}
    </div>
  );
}
