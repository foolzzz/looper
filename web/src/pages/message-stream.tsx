import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { LeadsTab } from "@/components/leads-tab";
import { TabStrip } from "@/components/tab-strip";
import { LABEL, SELECT } from "@/components/registry-ui";
import { leadsSettingsGet } from "@/lib/api-leads";
import {
  configureMessageStream, fetchMessageStream, fetchMessageStreamRuns, fetchMessageStreamChats,
  setMessageStreamChat, previewMessageStreamDeletion, deleteMessageStream, triageWhy,
  type MessageStreamRun, type MessageStreamSource, type MessageStreamChat,
  type MessageStreamDeletion, type MessageStreamDeletionPreview, type TriageWhy,
} from "@/lib/api";
import { sinceText } from "@/lib/gates";
import { cn } from "@/lib/utils";
import { startPolling } from "@/lib/polling";
import { errorText } from "@/lib/use-action";
import { formatBytes } from "@/lib/utils";

const REFRESH_MS = 10_000;

const HEALTH: Record<MessageStreamSource["health"], { label: string; variant: "success" | "warning" | "outline" | "destructive"; note: string }> = {
  healthy: { label: "Healthy", variant: "success", note: "The last pass finished and the stream is current." },
  degraded: { label: "Degraded", variant: "warning", note: "The last pass did not finish every file; its error is in the run list." },
  stale: { label: "Stale", variant: "destructive", note: "No pass has finished for over an hour." },
  off: { label: "Off", variant: "outline", note: "Nothing is being read. What was stored stays." },
};
const RUN_STATUS: Record<string, "success" | "warning" | "destructive" | "outline"> = {
  ok: "success", running: "outline", partial: "warning", interrupted: "warning",
};

const when = (iso: string | null) => (iso ? `${sinceText(iso) || "0 s"} ago` : "never");
const duration = (run: MessageStreamRun) => {
  if (!run.finished_at) return "";
  const seconds = Math.max(0, Math.round((Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000));
  return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
};

function Fact({ label, value, hint }: { label: string; value: React.ReactNode; hint?: string }) {
  return (
    <div className="min-w-0">
      <dt className={LABEL}>{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold tabular-nums">{value}</dd>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function SetupForm({ source, busy, onStart }: {
  source: MessageStreamSource | null; busy: boolean;
  onStart: (fields: { bridge_dir: string; start_from: "end" | "beginning"; user_open_id: string | null }) => void;
}) {
  const id = useId();
  const [dir, setDir] = useState(source?.bridge_dir ?? "");
  const [openId, setOpenId] = useState(source?.user_open_id ?? "");
  const [startFrom, setStartFrom] = useState<"end" | "beginning">(source?.start_from ?? "end");
  return (
    <form
      className="space-y-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (dir.trim()) onStart({ bridge_dir: dir.trim(), start_from: startFrom, user_open_id: openId.trim() || null });
      }}
    >
      <p className="text-sm text-muted-foreground">
        Looper can read the per-chat files of a collector that already runs on this machine, and keep the messages in
        its own database. It only reads those files; it does not call Lark.
      </p>
      <div className="space-y-1.5">
        <label htmlFor={`${id}-dir`} className={LABEL}>Collector directory (the one that holds chats/)</label>
        <Input id={`${id}-dir`} className="font-mono" placeholder="/path/to/collector" value={dir}
          onChange={(e) => setDir(e.target.value)} disabled={busy} />
      </div>
      <div className="space-y-1.5">
        <label htmlFor={`${id}-from`} className={LABEL}>Start from</label>
        <select id={`${id}-from`} value={startFrom} disabled={busy} onChange={(e) => setStartFrom(e.target.value as "end" | "beginning")}
          className={SELECT}>
          <option value="end">Now: only messages that arrive from here on</option>
          <option value="beginning">The beginning: import everything the files hold</option>
        </select>
        <p className="text-xs text-muted-foreground">
          Importing a large history takes a while and keeps raw messages in the database (a bot's after 30 days as text only).
        </p>
      </div>
      <div className="space-y-1.5">
        <label htmlFor={`${id}-open`} className={LABEL}>Your Lark open id (optional)</label>
        <Input id={`${id}-open`} className="font-mono" placeholder="ou_…" value={openId}
          onChange={(e) => setOpenId(e.target.value)} disabled={busy} />
        <p className="text-xs text-muted-foreground">Lets Looper mark which messages mention you.</p>
      </div>
      <Button type="submit" disabled={busy || !dir.trim()} className="min-h-11">
        {busy && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
        Start reading
      </Button>
    </form>
  );
}

function StatusCard({ source, busy, onSwitch }: { source: MessageStreamSource; busy: boolean; onSwitch: (on: boolean) => void }) {
  const health = HEALTH[source.health];
  const on = source.ingest_mode !== "off";
  return (
    <Card>
      <CardHeader className="flex-row flex-wrap items-center gap-3">
        <CardTitle className="text-base text-foreground">{source.name}</CardTitle>
        <Badge variant={health.variant}>{health.label}</Badge>
        <span className="flex-1" />
        <label className="flex min-h-11 cursor-pointer items-center gap-2 text-sm">
          <Switch checked={on} disabled={busy} onChange={onSwitch} label={`Read ${source.name}`} />
          <span>{on ? "Reading" : "Not reading"}</span>
        </label>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-muted-foreground">
          {health.note} Mode: {source.ingest_mode}, every pass on <span className="font-mono">{source.cron}</span> ({source.timezone}).
        </p>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          <Fact label="Messages stored" value={source.events.toLocaleString()} hint={`${source.events_last_hour.toLocaleString()} in the last hour`} />
          <Fact label="Newest message" value={when(source.newest_sent_at)} />
          <Fact label="Last good pass" value={when(source.last_ok_at)} />
          <Fact label="Chats" value={source.chats.toLocaleString()}
            hint={`${source.chats_excluded} excluded · ${source.chats_failing} failing`} />
        </dl>
        <p className="break-all font-mono text-xs text-muted-foreground">{source.bridge_dir}</p>
        <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          <Fact label="Raw storage" value={formatBytes(source.body_bytes)} hint={`${formatBytes(source.body_bytes_last_day)} added in 24 h`} />
          <Fact label="Daily events" value={source.events_last_day.toLocaleString()} hint="Ingested in the last 24 h" />
          <Fact label="Bodies expired" value={source.bodies_dropped.toLocaleString()} hint="Message text is kept" />
          <Fact label="Retention last ran" value={when(source.retention_last_at)} />
        </dl>
        <p className="text-xs text-muted-foreground">
          Retention: human raw messages {source.body_retention_days == null ? "kept indefinitely" : `kept for ${source.body_retention_days} days`};
          bot raw messages {source.app_body_retention_days == null ? "kept indefinitely" : `kept for ${source.app_body_retention_days} days`}.
          Expired raw bodies are cleared daily. Text is kept. This page never shows message text.
        </p>
      </CardContent>
    </Card>
  );
}

function RunList({ runs }: { runs: MessageStreamRun[] }) {
  if (runs.length === 0) return <p className="text-sm text-muted-foreground">No pass has run yet.</p>;
  return (
    <ul className="divide-y divide-border rounded-lg border border-border bg-card">
      {runs.map((run) => (
        <li key={run.run_id} className="space-y-1 px-4 py-3 text-sm">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Badge variant={RUN_STATUS[run.status] ?? "destructive"}>{run.status}</Badge>
            <span className="tabular-nums" title={run.started_at}>{when(run.started_at)}</span>
            <span className="text-muted-foreground">{run.mode}{run.trigger === "manual" ? " · manual" : ""}{duration(run) && ` · ${duration(run)}`}</span>
            <span className="flex-1" />
            <span className="tabular-nums">
              {run.events_new} new · {run.events_seen} already stored · {run.events_skipped} skipped · {run.skipped_by_deletion} skipped by deletion
              {run.files_rescanned ? ` · ${run.files_rescanned} file${run.files_rescanned === 1 ? "" : "s"} read again` : ""}
            </span>
          </div>
          {run.error && <p className="text-xs text-destructive [overflow-wrap:anywhere]">{run.error}</p>}
        </li>
      ))}
    </ul>
  );
}

function Chats({ sourceId, refresh }: { sourceId: string; refresh: number }) {
  const [chats, setChats] = useState<MessageStreamChat[]>([]);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => startPolling(async (signal) => {
    try {
      const result = await fetchMessageStreamChats(sourceId, query, signal);
      if (!signal.aborted) { setChats(result.chats); setError(""); setLoading(false); }
    } catch (err) {
      if (!signal.aborted) { setError(errorText(err)); setLoading(false); }
    }
  }, REFRESH_MS), [sourceId, query, attempt, refresh]);
  const include = async (chat: MessageStreamChat, included: boolean) => {
    setBusy(true);
    try {
      await setMessageStreamChat(sourceId, chat.chat_id, included);
      setAttempt((n) => n + 1);
    } catch (err) { setError(errorText(err)); }
    finally { setBusy(false); }
  };
  return (
    <Card>
      <CardHeader><CardTitle className="text-base text-foreground">Chats</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <form aria-label="Search chats" className="flex gap-2" onSubmit={(event) => { event.preventDefault(); setQuery(search.trim()); }}>
          <Input aria-label="Search chats" placeholder="Search chat name or ID" value={search} onChange={(event) => setSearch(event.target.value)} />
          <Button type="submit" variant="outline">Search</Button>
        </form>
        <p className="text-xs text-muted-foreground">Excluding a chat stops collection and removes it from every subscription window. Stored messages remain until deleted. Up to 200 matching chats are shown; search to narrow the list.</p>
        {error && <p role="alert" className="text-sm text-destructive [overflow-wrap:anywhere]">Cannot update chats ({error}) <Button variant="outline" size="sm" onClick={() => setAttempt((n) => n + 1)}>Retry chats</Button></p>}
        {loading ? <p role="status" className="text-sm text-muted-foreground">Loading chats…</p> : error && chats.length === 0 ? null : chats.length === 0 ?
          <p className="text-sm text-muted-foreground">No chats found.</p> : (
            <div role="region" aria-label="Chat table" tabIndex={0} className="overflow-x-auto focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">
              <table className="w-full min-w-xl text-left text-sm">
                <thead className="text-xs text-muted-foreground"><tr>
                  <th scope="col" className="px-2 py-2 font-medium">Chat</th>
                  <th scope="col" className="px-2 py-2 font-medium">State</th>
                  <th scope="col" className="px-2 py-2 text-right font-medium">7-day events</th>
                  <th scope="col" className="px-2 py-2 font-medium">Included</th>
                </tr></thead>
                <tbody className="divide-y divide-border">{chats.map((chat) => (
                  <tr key={chat.chat_id}>
                    <td className="min-w-40 max-w-48 px-2 py-3 [overflow-wrap:anywhere]">
                      <span className="font-medium">{chat.name || (chat.purged_at ? "Deleted chat" : "Unnamed chat")}</span>
                      <span className="block font-mono text-xs text-muted-foreground">{chat.chat_id}</span>
                      <span className="text-xs text-muted-foreground">{chat.kind}</span>
                    </td>
                    <td className="px-2 py-3"><Badge variant={chat.state === "failing" ? "warning" : "outline"}>{chat.state}</Badge></td>
                    <td className="px-2 py-3 text-right tabular-nums">{chat.events_7d.toLocaleString()}</td>
                    <td className="px-2 py-3">
                      <label className="flex min-h-11 items-center gap-2 text-xs">
                        <Switch checked={chat.inclusion === "included"} disabled={busy || !!chat.purged_at} label={`Include ${chat.name || chat.chat_id}`}
                          onChange={(included) => void include(chat, included)} />
                        {chat.purged_at ? "Deleted" : chat.inclusion === "included" ? "Included" : "Excluded"}
                      </label>
                    </td>
                  </tr>
                ))}</tbody>
              </table>
            </div>
          )}
      </CardContent>
    </Card>
  );
}

function Deletion({ sourceId, onDeleted }: { sourceId: string; onDeleted: () => void }) {
  const id = useId();
  const [scope, setScope] = useState<MessageStreamDeletion["scope"]>("message");
  const [messageId, setMessageId] = useState("");
  const [chatId, setChatId] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [review, setReview] = useState<{ target: MessageStreamDeletion; preview: MessageStreamDeletionPreview } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState("");
  const reviewHeading = useRef<HTMLHeadingElement>(null);
  const reviewButton = useRef<HTMLButtonElement>(null);
  const hadReview = useRef(false);
  useEffect(() => {
    if (review) reviewHeading.current?.focus();
    else if (hadReview.current) reviewButton.current?.focus();
    hadReview.current = !!review;
  }, [review]);
  const now = new Date();
  const maxDate = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const valid = scope === "message" ? !!messageId.trim() : scope === "chat" ? !!chatId.trim() :
    !!start && !!end && Date.parse(start) < Date.parse(end) && Date.parse(end) <= now.getTime();
  const preview = async () => {
    setBusy(true); setError(""); setResult("");
    try {
      const target: MessageStreamDeletion = { source_id: sourceId, scope,
        ...(scope === "message" ? { message_id: messageId.trim() } : scope === "chat" ? { chat_id: chatId.trim() } :
          { start: new Date(start).toISOString(), end: new Date(end).toISOString(), ...(chatId.trim() ? { chat_id: chatId.trim() } : {}) }) };
      setReview({ target, preview: await previewMessageStreamDeletion(target) });
    } catch (err) { setError(errorText(err)); }
    finally { setBusy(false); }
  };
  const remove = async () => {
    if (!review) return;
    setBusy(true); setError("");
    try {
      const response = await deleteMessageStream({ ...review.target, confirmation_token: review.preview.confirmation_token }, review.preview.session);
      setResult(`Deleted ${response.deleted} events. A deletion tombstone prevents re-import.`);
      setReview(null); onDeleted();
    } catch (err) { setError(errorText(err)); }
    finally { setBusy(false); }
  };
  return (
    <Card>
      <CardHeader><CardTitle className="text-base text-foreground">Delete stored messages</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        {error && <p role="alert" className="text-sm text-destructive [overflow-wrap:anywhere]">Deletion failed ({error})</p>}
        {result && <p role="status" className="text-sm">{result}</p>}
        {!review ? (
          <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); if (valid) void preview(); }}>
            <div className="space-y-1.5">
              <label className={LABEL} htmlFor={`${id}-scope`}>1. Choose what to delete</label>
              <select id={`${id}-scope`} className={SELECT} disabled={busy} value={scope} onChange={(event) => setScope(event.target.value as MessageStreamDeletion["scope"])}>
                <option value="message">One message</option><option value="range">Time range</option><option value="chat">Whole chat</option>
              </select>
            </div>
            {scope === "message" ? (
              <div className="space-y-1.5"><label className={LABEL} htmlFor={`${id}-message`}>Message ID</label>
                <Input id={`${id}-message`} aria-label="Message ID" required value={messageId} disabled={busy} onChange={(event) => setMessageId(event.target.value)} /></div>
            ) : (
              <div className="space-y-1.5"><label className={LABEL} htmlFor={`${id}-chat`}>Chat ID{scope === "range" ? " (optional; blank means all chats)" : ""}</label>
                <Input id={`${id}-chat`} aria-label="Chat ID" required={scope === "chat"} value={chatId} disabled={busy} onChange={(event) => setChatId(event.target.value)} /></div>
            )}
            {scope === "range" && <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5"><label className={LABEL} htmlFor={`${id}-start`}>From (inclusive, local time)</label>
                <Input id={`${id}-start`} type="datetime-local" max={maxDate} required value={start} disabled={busy} onChange={(event) => setStart(event.target.value)} /></div>
              <div className="space-y-1.5"><label className={LABEL} htmlFor={`${id}-end`}>Until (exclusive, local time)</label>
                <Input id={`${id}-end`} type="datetime-local" max={maxDate} required value={end} disabled={busy} onChange={(event) => setEnd(event.target.value)} /></div>
            </div>}
            <p className="text-xs text-muted-foreground">Whole-chat deletion also erases its name and excludes it. Deletions cannot be undone and block matching messages from future imports.</p>
            <Button ref={reviewButton} type="submit" variant="outline" disabled={busy || !valid}>Review deletion</Button>
          </form>
        ) : (
          <div className="space-y-3" aria-labelledby={`${id}-review`}>
            <h3 ref={reviewHeading} tabIndex={-1} id={`${id}-review`} className="text-sm font-medium focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring">2. Confirm deletion of {review.preview.events.toLocaleString()} events</h3>
            <p className="break-all text-sm">{review.target.scope === "message" ? `Message: ${review.target.message_id}` : review.target.scope === "chat" ? `Whole chat: ${review.target.chat_id}` :
              `${review.target.start} to ${review.target.end} (end excluded), ${review.target.chat_id || "all chats"}`}</p>
            <p className="text-sm font-medium">This deletion cannot reach:</p>
            <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              <li>Already pushed outputs in destination chats.</li>
              <li>Processor run outputs stored in run history.</li>
              <li>{review.preview.backups.count} upgrade backups; earliest: {review.preview.backups.earliest_at || "none"}. Older backups retain their contents until rotated away; schema-change backups now exclude message rows.</li>
              <li>When enabled, daily message backups keep the newest 7 copies; older copies age out through rotation. When disabled, no new message backup is created.</li>
              <li>The external lark-chat archive and other collector files.</li>
            </ul>
            <div className="flex flex-wrap gap-2">
              <Button variant="destructive" disabled={busy} onClick={() => void remove()}>Confirm deletion</Button>
              <Button variant="outline" disabled={busy} onClick={() => { setReview(null); setError(""); }}>Back</Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const WHY_CASE: Record<TriageWhy["case"], string> = {
  triage_off: "分流没开",
  out_of_scope: "不在分流范围",
  not_yet: "还没轮到这条消息",
  skipped: "这一段被跳过",
  event: "有事件引用",
  uncited: "忽略（未引用）",
  unknown: "无法判断",
};
const WHY_RULE: Record<NonNullable<TriageWhy["rule"]>, string> = {
  chat_excluded: "会话被排除",
  sender_kind: "发送人类型不符",
  imported_history: "导入的历史",
  own_app: "Looper 自己的应用",
};
const WHY_CATEGORY: Record<NonNullable<TriageWhy["category"]>, string> = {
  important: "重要", digest: "进摘要", ignore: "忽略",
};

function whyNext(answer: TriageWhy): string {
  if (answer.case === "triage_off") return answer.cause === "no_subscription" ? "先建一条分流订阅。" : "分流开关是关的。";
  if (answer.case === "out_of_scope") return `范围规则：${answer.rule ? WHY_RULE[answer.rule] : "没有对上的规则"}`;
  if (answer.case === "not_yet") return answer.next_run_at ? `下次运行：${answer.next_run_at}` : "没有下次运行。";
  if (answer.case === "skipped") return `跳过原因：${answer.skip_reason ?? ""}`;
  if (answer.case === "event") return answer.handling ?? "这条消息已有事件。";
  if (answer.case === "uncited") return "处理器没有提到这条消息。";
  return `已核对：${(answer.checked ?? []).join("、")}`;
}

/** Look up one message. The stream has no per-message list yet, so this is a paste box rather than a label on each row. */
function WhyLookup() {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [answer, setAnswer] = useState<TriageWhy | null>(null);
  const ask = async (event: FormEvent) => {
    event.preventDefault();
    const ref = message.trim();
    if (!ref) return;
    setBusy(true);
    setError("");
    try {
      setAnswer(await triageWhy(ref));
    } catch (err) {
      setAnswer(null);
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const conclusion = answer == null ? "" : answer.case === "event" && answer.category
    ? `${WHY_CASE.event}（${WHY_CATEGORY[answer.category]}）` : WHY_CASE[answer.case];
  return (
    <section aria-labelledby="why-title">
      <Card>
        <CardHeader><CardTitle id="why-title" className="text-base text-foreground">为什么没告诉我</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <form aria-label="为什么没告诉我" className="space-y-3" onSubmit={(event) => void ask(event)}>
            <div className="space-y-1.5">
              <label htmlFor="why-message" className={LABEL}>消息编号或链接</label>
              <Input id="why-message" aria-label="消息编号或链接" className="font-mono" value={message} disabled={busy}
                onChange={(event) => setMessage(event.target.value)} />
            </div>
            <Button type="submit" disabled={busy || !message.trim()} className="min-h-11">
              {busy && <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />}
              查询
            </Button>
          </form>
          {error && <p role="alert" className="text-sm text-destructive [overflow-wrap:anywhere]">{error}</p>}
          {answer && (
            <dl className="space-y-2 text-sm">
              <div>
                <dt className={LABEL}>结论</dt>
                <dd>{conclusion}</dd>
              </div>
              {answer.event_id && (
                <div>
                  <dt className={LABEL}>事件</dt>
                  <dd className="font-mono">{answer.event_id}</dd>
                </div>
              )}
              {answer.reason && (
                <div>
                  <dt className={LABEL}>理由</dt>
                  <dd className="[overflow-wrap:anywhere]">{answer.reason}</dd>
                </div>
              )}
              <div>
                <dt className={LABEL}>下一步</dt>
                <dd className="[overflow-wrap:anywhere]">{whyNext(answer)}</dd>
              </div>
            </dl>
          )}
        </CardContent>
      </Card>
    </section>
  );
}

/** The message stream: one source, whether it is being read, how current it is, and its recent passes. */
export function MessageStreamPage() {
  const [sources, setSources] = useState<MessageStreamSource[] | null>(null);
  const [runs, setRuns] = useState<MessageStreamRun[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [leadsOn, setLeadsOn] = useState(false);
  const [tab, setTab] = useState<"stream" | "leads">("stream");
  useEffect(() => {
    let live = true;
    leadsSettingsGet().then(
      (settings) => { if (live) setLeadsOn(settings.enabled === true); },
      () => { if (live) setLeadsOn(false); },
    );
    return () => { live = false; };
  }, []);

  useEffect(() => {
    return startPolling(async (signal) => {
      try {
        const { sources: found } = await fetchMessageStream(signal);
        const first = found[0];
        const read = first ? (await fetchMessageStreamRuns(first.source_id, signal)).runs : [];
        setSources(found);
        setRuns(read);
        setError("");
      } catch (err) {
        if (!signal.aborted) setError(errorText(err));
      }
    }, REFRESH_MS);
  }, [attempt]);

  const apply = useCallback(async (fields: Parameters<typeof configureMessageStream>[0]) => {
    setBusy(true);
    setError("");
    try {
      await configureMessageStream(fields);
      setAttempt((n) => n + 1);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }, []);

  const source = sources?.[0] ?? null;
  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <div className="space-y-1">
        <h1 className="text-xl font-semibold">Message stream</h1>
        <p className="text-sm text-muted-foreground">Messages Looper has read into its own database, for subscriptions to use.</p>
      </div>
      {leadsOn && (
        <TabStrip label="Message stream" selected={tab} className="gap-1 border-b border-border [--gutter:--spacing(6)]">
          {([["stream", "消息"], ["leads", "线索"]] as const).map(([id, text]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
              className={cn("-mb-px border-b-2 px-3 py-2 text-sm font-medium pointer-coarse:min-h-11 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
                tab === id ? "border-primary text-foreground" : "border-transparent text-muted-foreground")}>{text}</button>
          ))}
        </TabStrip>
      )}
      {leadsOn && tab === "leads" ? <LeadsTab /> : null}
      {(!leadsOn || tab === "stream") && error && (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-destructive [overflow-wrap:anywhere]">
          <span>{sources ? "The last action failed" : "Cannot load the message stream"} ({error})</span>
          <Button type="button" variant="outline" size="sm" className="min-h-11" onClick={() => setAttempt((n) => n + 1)}>Retry</Button>
        </div>
      )}
      {(!leadsOn || tab === "stream") && !sources && !error && (
        <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" /> Loading…
        </p>
      )}
      {(!leadsOn || tab === "stream") && sources && !source && (
        <Card><CardHeader><CardTitle className="text-base text-foreground">Set up a source</CardTitle></CardHeader>
          <CardContent><SetupForm source={null} busy={busy} onStart={(f) => void apply({ ingest_mode: "bridge", ...f })} /></CardContent></Card>
      )}
      {(!leadsOn || tab === "stream") && source && (
        <>
          <StatusCard source={source} busy={busy}
            onSwitch={(on) => void apply({ source_id: source.source_id, ingest_mode: on ? "bridge" : "off" })} />
          {source.ingest_mode === "off" && !source.bridge_dir && (
            <Card><CardContent className="pt-5"><SetupForm source={source} busy={busy}
              onStart={(f) => void apply({ source_id: source.source_id, ingest_mode: "bridge", ...f })} /></CardContent></Card>
          )}
          <Chats sourceId={source.source_id} refresh={attempt} />
          <Deletion key={source.source_id} sourceId={source.source_id} onDeleted={() => setAttempt((n) => n + 1)} />
          <section aria-labelledby="runs-title" className="space-y-2">
            <h2 id="runs-title" className="text-sm font-medium text-muted-foreground">Recent passes</h2>
            <RunList runs={runs} />
          </section>
        </>
      )}
      {(!leadsOn || tab === "stream") && <WhyLookup />}
    </div>
  );
}
