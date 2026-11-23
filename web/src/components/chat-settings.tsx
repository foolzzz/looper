import { AlertTriangle } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  fetchChatSettings, fetchCompletionSummary, updateChatSettings, type ChatSettings, type CompletionSummary,
  fetchSessionRoutingSettings, updateSessionRoutingSettings, type SessionRoutingSettings, type SessionRoutingSettingsUpdate,
  fetchOutboundGateway, updateOutboundGateway, type OutboundGatewaySettings,
  type CompletionTurns, type ChatSettingsUpdate, type MissingOperatorBot,
  fetchTurnContextSettings, updateTurnContextSettings, type TurnContextSettings, type TurnContextSettingsUpdate,
  fetchManagerMetricsSettings, updateManagerMetricsSettings, type ManagerMetricsSettings,
  fetchManagerPersona, updateManagerPersona, type ManagerPersona,
} from "@/lib/api";
import { errorText } from "@/lib/use-action";

const SELECT = "mt-1 w-full max-w-sm rounded-md border border-input bg-transparent px-3 py-1.5 text-sm text-foreground "
  + "pointer-coarse:h-11 pointer-coarse:text-base focus:outline-none focus:ring-2 focus:ring-primary/50";
const PERSONA_TOTAL = 600;
const PERSONA_FIELDS = [
  { key: "name", label: "名字", placeholder: "管家", limit: 20 },
  { key: "address", label: "称呼", placeholder: "你", limit: 20 },
  { key: "tone", label: "语气", placeholder: "简洁、直接", limit: 200 },
  { key: "notes", label: "补充说明", limit: 400 },
] as const;

const LEDGER_NOTICE = "这些机器人的私聊里，你做的决定会记成转述，直到你在 Settings → Channels 填上『我的 ID』";
const ROUTING_NOTICE = "这些机器人的私聊暂时留在主会话里，直到你在 Settings → Channels 填上『我的 ID』";
const MAX_RESIDENT_ERROR = "常驻会话数要是 1 到 16 的整数";

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

/** Settings → Chat: whether a reply can be stopped (Esc, the Stop button, /stop; experimental, off by default),
 * whether the Manager tells a direct chat or the dashboard chat that its task ended (off by default), with the last
 * 24 hours of task ends by how they were told, and the default Manager's persona (unset until the operator sets it). */
export function ChatSettingsSection() {
  const id = useId();
  const [settings, setSettings] = useState<ChatSettings | null>(null);
  const [outbound, setOutbound] = useState<OutboundGatewaySettings | null>(null);
  const [outboundError, setOutboundError] = useState("");
  const [outboundAttempt, setOutboundAttempt] = useState(0);
  const [outboundSaving, setOutboundSaving] = useState(false);
  const [summary, setSummary] = useState<CompletionSummary | null>(null);
  const [summaryError, setSummaryError] = useState("");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [saving, setSaving] = useState(false);
  const [context, setContext] = useState<TurnContextSettings | null>(null);
  const [contextError, setContextError] = useState("");
  const [timezone, setTimezone] = useState("");
  const [metrics, setMetrics] = useState<ManagerMetricsSettings | null>(null);
  const [metricsError, setMetricsError] = useState("");
  const [metricsSaving, setMetricsSaving] = useState(false);
  const [missingBots, setMissingBots] = useState<MissingOperatorBot[]>([]);
  const [draft, setDraft] = useState<ManagerPersona | null>(null);
  const [personaError, setPersonaError] = useState("");
  const [savingPersona, setSavingPersona] = useState(false);
  const [routing, setRouting] = useState<SessionRoutingSettings | null>(null);
  const [routingError, setRoutingError] = useState("");
  const [routingBots, setRoutingBots] = useState<MissingOperatorBot[]>([]);
  const [residentDraft, setResidentDraft] = useState<string | null>(null);
  const [residentError, setResidentError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchChatSettings().then((value) => {
      if (alive) {
        setSettings(value);
      }
    }).catch((err: unknown) => {
      if (alive) setError(errorText(err));
    });
    fetchManagerPersona().then((value) => {
      if (alive) setDraft(value);
    }).catch((err: unknown) => {
      if (alive) setPersonaError(errorText(err));
    });
    fetchSessionRoutingSettings().then((value) => {
      if (alive) { setRouting(value); setRoutingError(""); }
    }).catch((err: unknown) => {
      if (alive) setRoutingError(errorText(err));
    });
    fetchTurnContextSettings().then((value) => {
      if (alive) {
        setContext(value);
        setTimezone(value.timezone ?? "");
        setContextError("");
      }
    }).catch((err: unknown) => {
      if (alive) setContextError(errorText(err));
    });
    fetchManagerMetricsSettings().then((value) => {
      if (alive) setMetrics(value);
    }).catch((err: unknown) => {
      if (alive) setMetricsError(errorText(err));
    });
    fetchCompletionSummary().then((value) => {
      if (alive) setSummary(value);
    }).catch((err: unknown) => {
      if (alive) setSummaryError(errorText(err));
    });
    return () => { alive = false; };
  }, [attempt]);

  useEffect(() => {
    let alive = true;
    fetchOutboundGateway().then((value) => {
      if (alive) { setOutbound(value); setOutboundError(""); }
    }).catch((err: unknown) => {
      if (alive) setOutboundError(errorText(err));
    });
    return () => { alive = false; };
  }, [outboundAttempt]);

  const save = async (body: ChatSettingsUpdate) => {
    setSaving(true);
    setError("");
    try {
      const next = await updateChatSettings(body);
      setSettings(next);
      if ("ledger_enabled" in body) {
        setMissingBots(body.ledger_enabled && next.missing_operator_ids?.length ? next.missing_operator_ids : []);
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const savePersona = async () => {
    if (!draft) return;
    setSavingPersona(true);
    setPersonaError("");
    try {
      setDraft(await updateManagerPersona(draft));
    } catch (err) {
      setPersonaError(errorText(err));
    } finally {
      setSavingPersona(false);
    }
  };

  const saveRouting = async (body: SessionRoutingSettingsUpdate) => {
    setSaving(true);
    setRoutingError("");
    try {
      const next = await updateSessionRoutingSettings(body);
      setRouting(next);
      if ("max_resident" in body) setResidentDraft(null);
      if ("session_routing_enabled" in body) {
        setRoutingBots(body.session_routing_enabled && next.missing_operator_ids?.length ? next.missing_operator_ids : []);
      }
    } catch (err) {
      setRoutingError(errorText(err));
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
    void saveRouting({ max_resident: value });
  };

  const saveOutbound = async (enabled: boolean) => {
    setOutboundSaving(true);
    setOutboundError("");
    try {
      setOutbound(await updateOutboundGateway({ outbound_enabled: enabled }));
    } catch (err) {
      setOutboundError(errorText(err));
    } finally {
      setOutboundSaving(false);
    }
  };

  const saveContext = async (body: TurnContextSettingsUpdate) => {
    setSaving(true);
    setContextError("");
    try {
      const next = await updateTurnContextSettings(body);
      setContext((previous) => ({ ...next, missing_operator_ids:
        "turn_context_enabled" in body ? next.missing_operator_ids : previous?.missing_operator_ids }));
    } catch (err) {
      setContextError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const saveMetrics = async (enabled: boolean) => {
    if (!confirm(enabled ? "开启管家度量？这会记录回合延迟和主动消息的送达与回复。" : "关闭管家度量？这会停止采集，已有记录保留。")) return;
    setMetricsSaving(true);
    setMetricsError("");
    try {
      setMetrics(await updateManagerMetricsSettings({ enabled }));
    } catch (err) {
      setMetricsError(errorText(err));
    } finally {
      setMetricsSaving(false);
    }
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
              <input id={`${id}-routing`} type="checkbox" checked={routing?.session_routing_enabled ?? false} disabled={saving || !routing}
                     aria-describedby={`${id}-routing-help`}
                     onChange={(event) => void saveRouting({ session_routing_enabled: event.target.checked })} />
              分会话：群和其他人的私聊各用一个会话
            </label>
            <p id={`${id}-routing-help`} className="text-xs text-muted-foreground">
              默认关闭。只对默认管家生效：打开后每个飞书/Telegram 群和每个其他允许发送者的私聊有自己的会话，与主会话并行运行；关掉后全部回到主会话。
            </p>
            {routingError && <p role="alert" className="break-words text-sm text-destructive">{routingError}</p>}
            {routingBots.length > 0 && <BotsNotice text={ROUTING_NOTICE} bots={routingBots} />}
            {routing?.session_routing_enabled && (
              <div className="space-y-1 pt-1">
                <label htmlFor={`${id}-resident`} className="block text-sm font-medium">常驻会话数</label>
                <div className="flex flex-wrap items-center gap-2">
                  <input id={`${id}-resident`} type="number" inputMode="numeric" min={1} max={16} step={1}
                         value={residentDraft ?? String(routing.max_resident)} disabled={saving}
                         aria-describedby={`${id}-resident-help`} aria-invalid={residentError ? true : undefined}
                         className={SELECT.replace("mt-1 w-full max-w-sm", "w-24") + " tabular-nums"}
                         onChange={(event) => { setResidentDraft(event.target.value); setResidentError(""); }} />
                  <Button variant="outline" disabled={saving || residentDraft === null} onClick={saveResident}>Save</Button>
                  <Button variant="outline" disabled={saving}
                          onClick={() => { setResidentError(""); void saveRouting({ max_resident: null }); }}>
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
          <div className="space-y-1.5">
            <label htmlFor={`${id}-metrics`} className="flex items-center gap-2 text-sm font-medium">
              <input id={`${id}-metrics`} type="checkbox" checked={metrics?.enabled ?? false}
                     disabled={!metrics || metricsSaving} aria-describedby={`${id}-metrics-help`}
                     onChange={(event) => void saveMetrics(event.target.checked)} />
              记录管家体验指标
            </label>
            <p id={`${id}-metrics-help`} className="text-xs text-muted-foreground">
              每轮记下回执、首条回复、工具调用次数和是否交付；主动消息记一行。只记录，不改变管家的行为。
            </p>
            {metricsError && <p role="alert" className="break-words text-sm text-destructive">{metricsError}</p>}
          </div>
          {context && <>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-context`} className="flex items-center gap-2 text-sm font-medium">
                <input id={`${id}-context`} type="checkbox" aria-label="每轮上下文"
                       checked={context.turn_context_enabled} disabled={saving}
                       aria-describedby={`${id}-context-help`}
                       onChange={(event) => void saveContext({ turn_context_enabled: event.target.checked })} />
                每轮上下文
              </label>
              <p id={`${id}-context-help`} className="text-xs text-muted-foreground">
                默认关闭。设置会保存；每轮上下文功能接入后，管家才会获得本地时间、说话人、聊天来源和回复语言，并检查授权。
              </p>
              {context.turn_context_enabled && (context.missing_operator_ids?.length ?? 0) > 0 && (
                <div role="alert" className="space-y-1 text-sm text-muted-foreground">
                  <p>以下机器人没有设置「我的 ID」，无法识别操作者；需要操作者授权的动作会被拒绝：</p>
                  <ul className="list-disc space-y-1 pl-5">
                    {context.missing_operator_ids!.map((bot) => (
                      <li key={bot.channel_id} className="break-words">{bot.name} ({bot.channel_type})</li>
                    ))}
                  </ul>
                  <a href="#settings/channels" className="text-primary hover:underline">前往渠道设置</a>
                </div>
              )}
            </div>
            <form className="space-y-1.5" onSubmit={(event) => {
              event.preventDefault();
              void saveContext({ timezone: timezone.trim() || null });
            }}>
              <label htmlFor={`${id}-timezone`} className="block text-sm font-medium">时区</label>
              <div className="flex max-w-sm items-center gap-2">
                <Input id={`${id}-timezone`} aria-label="时区" value={timezone} disabled={saving}
                       placeholder={context.server_timezone} aria-describedby={`${id}-timezone-help`}
                       onChange={(event) => setTimezone(event.target.value)} />
                <Button type="submit" variant="outline" disabled={saving}>保存</Button>
              </div>
              <p id={`${id}-timezone-help`} className="text-xs text-muted-foreground">
                填写 IANA 时区名（如 Asia/Shanghai）；留空恢复服务器时区。现有定时任务的时区不变。
              </p>
            </form>
            <div className="space-y-1.5">
              <label htmlFor={`${id}-language`} className="block text-sm font-medium">回复语言</label>
              <select id={`${id}-language`} aria-label="回复语言" value={context.reply_language} disabled={saving}
                      className={SELECT} onChange={(event) => void saveContext({
                        reply_language: event.target.value as "zh-CN" | "en",
                      })}>
                <option value="zh-CN">中文</option>
                <option value="en">English</option>
              </select>
            </div>
          </>}
          {contextError && <div className="space-y-2">
            <p role="alert" className="break-words text-sm text-destructive">每轮上下文设置不可用：{contextError}</p>
            {!context && <Button variant="outline" onClick={() => setAttempt((n) => n + 1)}>重试</Button>}
          </div>}
          {outbound && <div className="space-y-1.5">
            <label htmlFor={`${id}-outbound`} className="flex items-center gap-2 text-sm font-medium">
              <input id={`${id}-outbound`} type="checkbox" checked={outbound.outbound_enabled} disabled={outboundSaving}
                     aria-describedby={`${id}-outbound-help`}
                     onChange={(event) => void saveOutbound(event.target.checked)} />
              出站统一限流和重试（飞书、Telegram）
            </label>
            <p id={`${id}-outbound-help`} className="text-xs text-muted-foreground">
              默认关闭。当前接入飞书，发送前保存，遇到超时会按幂等键重试；Telegram 将在后续接入。
            </p>
          </div>}
          {outboundError && <div className="space-y-3">
            <p role="alert" className="break-words text-sm text-destructive">出站设置不可用：{outboundError}</p>
            <Button variant="outline" disabled={outboundSaving} onClick={() => setOutboundAttempt((n) => n + 1)}>重试出站设置</Button>
          </div>}
          <PersonaFields id={id} draft={draft} error={personaError} saving={savingPersona}
                         onDraft={setDraft} onSave={() => void savePersona()} />
          {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
        </div>
      )}
    </Card>
  );
}

function PersonaFields({ id, draft, error, saving, onDraft, onSave }: {
  id: string;
  draft: ManagerPersona | null;
  error: string;
  saving: boolean;
  onDraft: (value: ManagerPersona) => void;
  onSave: () => void;
}) {
  if (!draft) {
    return error
      ? <p role="alert" className="break-words text-sm text-destructive">{error}</p>
      : <p role="status" className="text-sm text-muted-foreground">Loading…</p>;
  }
  const total = PERSONA_FIELDS.reduce((sum, field) => sum + draft[field.key].length, 0);
  return (
    <div className="space-y-3 border-t border-border pt-5">
      <div className="space-y-1">
        <p className="text-sm font-medium">管家人格</p>
        <p className="text-xs text-muted-foreground">可保存默认管家的人格设置；目前尚未用于提示词，后续接入后生效。</p>
      </div>
      {PERSONA_FIELDS.map((field) => (
        <div key={field.key} className="space-y-1">
          <div className="flex items-baseline justify-between gap-3">
            <label htmlFor={`${id}-persona-${field.key}`} className="text-sm font-medium">{field.label}</label>
            <span className="text-xs tabular-nums text-muted-foreground">{draft[field.key].length}/{field.limit}</span>
          </div>
          <Input id={`${id}-persona-${field.key}`} value={draft[field.key]} maxLength={field.limit}
                 placeholder={"placeholder" in field ? field.placeholder : undefined} disabled={saving}
                 onChange={(event) => onDraft({ ...draft, [field.key]: event.target.value })} />
        </div>
      ))}
      <p className={`text-xs tabular-nums ${total > PERSONA_TOTAL ? "text-destructive" : "text-muted-foreground"}`}>
        合计 {total}/{PERSONA_TOTAL}
      </p>
      {error && <p role="alert" className="break-words text-sm text-destructive">{error}</p>}
      <Button type="button" disabled={saving} onClick={onSave}>保存</Button>
    </div>
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
