import { AlertTriangle } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  fetchChatSettings, fetchCompletionSummary, updateChatSettings, type ChatSettings, type CompletionSummary,
  type CompletionTurns, type MissingOperatorBot,
} from "@/lib/api";
import { errorText } from "@/lib/use-action";

const SELECT = "mt-1 w-full max-w-sm rounded-md border border-input bg-transparent px-3 py-1.5 text-sm text-foreground "
  + "pointer-coarse:h-11 pointer-coarse:text-base focus:outline-none focus:ring-2 focus:ring-primary/50";

const LEDGER_NOTICE = "这些机器人的私聊里，你做的决定会记成转述，直到你在 Settings → Channels 填上『我的 ID』";
const ROUTING_NOTICE = "这些机器人的私聊暂时留在主会话里，直到你在 Settings → Channels 填上『我的 ID』";
const MAX_RESIDENT_ERROR = "常驻会话数要是 1 到 16 的整数";

type ChatSettingsUpdate = {
  stop_enabled?: boolean; completion_turns?: CompletionTurns; ledger_enabled?: boolean;
  session_routing_enabled?: boolean; max_resident?: number | null;
};

/** Why a completion turn's events got the code notice instead, in words (``manager_events.reason``). */
const FALLBACK_REASONS: Record<string, string> = {
  turn_failed: "the turn failed",
  turn_timeout: "it took over 2 minutes",
  turn_stopped: "stopped",
  empty_reply: "no reply",
  send_failed: "not sent",
  queue_deadline: "waited 15 minutes",
  restart: "the server restarted",
  manager_off: "Manager off",
  off: "turned off",
  target_changed: "the schedule's target changed",
};

/** Settings → Chat: whether a reply can be stopped (Esc, the Stop button, /stop; experimental, off by default), and
 * whether the Manager tells a direct chat or the dashboard chat that its task ended (off by default), with the last
 * 24 hours of task ends by how they were told. */
export function ChatSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<ChatSettings | null>(null);
  const [summary, setSummary] = useState<CompletionSummary | null>(null);
  const [summaryError, setSummaryError] = useState("");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [missingBots, setMissingBots] = useState<MissingOperatorBot[]>([]);
  const [routingBots, setRoutingBots] = useState<MissingOperatorBot[]>([]);
  const [residentDraft, setResidentDraft] = useState<string | null>(null);
  const [residentError, setResidentError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchChatSettings().then((value) => {
      if (alive) setSettings(value);
    }).catch((err: unknown) => {
      if (alive) setError(errorText(err));
    });
    fetchCompletionSummary().then((value) => {
      if (alive) setSummary(value);
    }).catch((err: unknown) => {
      if (alive) setSummaryError(errorText(err));
    });
    return () => { alive = false; };
  }, [attempt]);

  const save = async (body: ChatSettingsUpdate) => {
    setSaving(true);
    setError("");
    try {
      const next = await updateChatSettings(body);
      setSettings(next);
      if ("max_resident" in body) setResidentDraft(null);
      if ("session_routing_enabled" in body) {
        setRoutingBots(body.session_routing_enabled && next.missing_operator_ids?.length ? next.missing_operator_ids : []);
      }
      if ("ledger_enabled" in body) {
        setMissingBots(body.ledger_enabled && next.missing_operator_ids?.length ? next.missing_operator_ids : []);
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const saveResident = () => {
    const text = (residentDraft ?? "").trim();
    const value = Number(text);
    if (!/^\d+$/.test(text) || value < 1 || value > 16) {
      setResidentError(MAX_RESIDENT_ERROR);
      return;
    }
    setResidentError("");
    void save({ max_resident: value });
  };

  return (
    <Card className="min-w-0 p-5">
      {!settings ? (
        error ? (
          <div className="space-y-3">
            <p role="alert" className="break-words text-sm text-destructive">{error}</p>
            <Button variant="outline" onClick={() => { setError(""); setAttempt((n) => n + 1); }}>Retry</Button>
          </div>
        ) : (
          <p role="status" className="text-sm text-muted-foreground">Loading…</p>
        )
      ) : (
        <div className="space-y-5">
          <div className="space-y-1.5">
            <label htmlFor={`${id}-stop`} className="flex items-center gap-2 text-sm font-medium">
              <input id={`${id}-stop`} type="checkbox" checked={settings.stop_enabled} disabled={saving}
                     aria-describedby={`${id}-stop-help`}
                     onChange={(event) => void save({ stop_enabled: event.target.checked })} />
              Allow stopping a reply
            </label>
            <p id={`${id}-stop-help`} className="text-xs text-muted-foreground">
              Experimental, off by default: Esc, the Stop button, and /stop on Telegram and Lark. Telegram's command menu
              lists /stop after the server restarts.
            </p>
          </div>
          <div className="space-y-1.5">
            <label htmlFor={`${id}-completion`} className="block text-sm font-medium">
              When a task ends
            </label>
            <select id={`${id}-completion`} value={settings.completion_turns} disabled={saving}
                    aria-describedby={`${id}-completion-help`} className={SELECT}
                    onChange={(event) => void save({ completion_turns: event.target.value as CompletionTurns })}>
              <option value="off">Send the task notice</option>
              <option value="dm_only">The Manager tells the chat</option>
            </select>
            <p id={`${id}-completion-help`} className="text-xs text-muted-foreground">
              Off by default. On, in direct chats and the dashboard chat the Manager writes the message in a read-only
              turn a minute after the first task ends, for the tasks that end by then, and Looper adds each task's
              link. Group chats always get the task notice, and so does a chat whose turn fails or takes over 2
              minutes.
            </p>
            <CompletionCounts summary={summary} error={summaryError} />
          </div>
          <div className="space-y-1.5">
            <label htmlFor={`${id}-ledger`} className="flex items-center gap-2 text-sm font-medium">
              <input id={`${id}-ledger`} type="checkbox" checked={settings.ledger_enabled} disabled={saving}
                     aria-describedby={`${id}-ledger-help`}
                     onChange={(event) => void save({ ledger_enabled: event.target.checked })} />
              账本：登记等你确认的事、决定和谁在做什么
            </label>
            <p id={`${id}-ledger-help`} className="text-xs text-muted-foreground">
              默认关闭。打开后开始登记，默认管家得到账本工具；关掉后不再登记，已经记下的还在。
            </p>
            {missingBots.length > 0 && <BotsNotice text={LEDGER_NOTICE} bots={missingBots} />}
          </div>
          <div className="space-y-1.5">
            <label htmlFor={`${id}-routing`} className="flex items-center gap-2 text-sm font-medium">
              <input id={`${id}-routing`} type="checkbox" checked={settings.session_routing_enabled} disabled={saving}
                     aria-describedby={`${id}-routing-help`}
                     onChange={(event) => void save({ session_routing_enabled: event.target.checked })} />
              分会话：群和其他人的私聊各用一个会话
            </label>
            <p id={`${id}-routing-help`} className="text-xs text-muted-foreground">
              默认关闭。只对默认管家生效：打开后每个飞书/Telegram 群和每个其他允许发送者的私聊有自己的会话，与主会话并行运行；关掉后全部回到主会话。
            </p>
            {routingBots.length > 0 && <BotsNotice text={ROUTING_NOTICE} bots={routingBots} />}
            {settings.session_routing_enabled && (
              <div className="space-y-1 pt-1">
                <label htmlFor={`${id}-resident`} className="block text-sm font-medium">常驻会话数</label>
                <div className="flex flex-wrap items-center gap-2">
                  <input id={`${id}-resident`} type="number" inputMode="numeric" min={1} max={16} step={1}
                         value={residentDraft ?? String(settings.max_resident)} disabled={saving}
                         aria-describedby={`${id}-resident-help`} aria-invalid={residentError ? true : undefined}
                         className={SELECT.replace("mt-1 w-full max-w-sm", "w-24") + " tabular-nums"}
                         onChange={(event) => { setResidentDraft(event.target.value); setResidentError(""); }} />
                  <Button variant="outline" disabled={saving || residentDraft === null} onClick={saveResident}>Save</Button>
                  <Button variant="outline" disabled={saving}
                          onClick={() => { setResidentError(""); void save({ max_resident: null }); }}>
                    Restore default
                  </Button>
                </div>
                <p id={`${id}-resident-help`} className="text-xs text-muted-foreground">
                  1 到 16，默认 2。最多这么多个会话保持进程运行，超出的空闲会话先停掉，下一条消息来时接着聊；1 表示只有主会话常驻。
                </p>
                {residentError && <p role="alert" className="text-sm text-destructive">{residentError}</p>}
              </div>
            )}
          </div>
          {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
        </div>
      )}
    </Card>
  );
}

function BotsNotice({ text, bots }: { text: string; bots: MissingOperatorBot[] }) {
  const [before, after] = text.split("Settings → Channels");
  return (
    <div role="status" className="space-y-1 rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-sm text-foreground">
      <p>
        {before}
        <a href="#settings/channels" className="underline underline-offset-2">Settings → Channels</a>
        {after}
      </p>
      <ul className="list-disc pl-5">
        {bots.map((bot) => <li key={bot.channel_id}>{bot.name.trim() || bot.channel_id}</li>)}
      </ul>
    </div>
  );
}

function CompletionCounts({ summary, error }: { summary: CompletionSummary | null; error: string }) {
  if (error) return <p className="text-xs text-muted-foreground">Task-end counts are unavailable: {error}</p>;
  if (!summary) return null;
  const told = summary.by_state.delivered ?? 0;
  const fallback = summary.by_state.fallback ?? 0;
  const notices = summary.by_state.skipped ?? 0;
  const reasons = Object.entries(summary.by_reason)
    .filter(([reason]) => reason in FALLBACK_REASONS && reason !== "off")
    .map(([reason, count]) => `${FALLBACK_REASONS[reason]} ${count}`);
  return (
    <div className="space-y-1.5 pt-1">
      <p className="text-xs text-muted-foreground">
        Last {summary.window_hours} hours:{" "}
        <span className="tabular-nums text-foreground/90">{told}</span> told by the Manager ·{" "}
        <span className="tabular-nums text-foreground/90">{fallback}</span> sent as the notice after a turn
        {fallback > 0 && reasons.length > 0 ? ` (${reasons.join(", ")})` : ""} ·{" "}
        <span className="tabular-nums text-foreground/90">{notices}</span> {notices === 1 ? "notice" : "notices"}
        {" "}without a turn ·{" "}
        <span className="tabular-nums text-foreground/90">${summary.turn_cost_usd.toFixed(2)}</span> on turns
        {summary.pending > 0 ? ` · ${summary.pending} waiting` : ""}
      </p>
      {summary.failing && (
        <Badge variant="warning" className="gap-1">
          <AlertTriangle className="h-3 w-3" aria-hidden="true" />
          The last 5 turns all fell back to the notice
        </Badge>
      )}
    </div>
  );
}
