import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Bell,
  Bot,
  CheckCircle2,
  ChevronDown,
  Circle,
  CircleMinus,
  CircleX,
  Info,
  Loader2,
  MessageCircleQuestionMark,
  Send,
  User,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Markdown } from "@/components/markdown";
import {
  GATE_MESSAGE_MAX_CHARS,
  endGateDiscussion,
  fetchGateThread,
  isActiveTask,
  postGateMessage,
  type GateAgendaItem,
  type GateMessage,
  type TaskDetail,
  type UserGate,
} from "@/lib/api";
import { gateAnchor, gateLook, gatePostsText, gateWaitsOn, isGateShown, isGateWaiting, sinceText } from "@/lib/gates";
import { errorText, useAction } from "@/lib/use-action";
import { startPolling } from "@/lib/polling";
import { cn } from "@/lib/utils";

/** How often a gate that waits reads its discussion again while its task runs. */
const THREAD_POLL_MS = 3000;

type GateLanguage = { language?: "zh-CN" };

// Big enough to hit (44 px), wrapping a long label instead of widening the page (also within a word with no spaces),
// with a focus ring that shows.
const TARGET = "h-auto min-h-11 max-w-full whitespace-normal [overflow-wrap:anywhere] py-2 text-left focus-visible:ring-2";

/** A gate as a badge: its state's word and tone, or whose turn it is while it is discussed (the Planner's only while
 * its task runs, `active`). */
export function GateBadge({ gate, active, language }: { gate: Pick<UserGate, "state" | "waiting_on">; active: boolean } & GateLanguage) {
  const look = gateLook(gate, active, language);
  return <Badge variant={look.variant}>{look.label}</Badge>;
}

/** The time of day for today, else the date and time. */
function whenText(date: Date, language?: "zh-CN"): string {
  return date.toDateString() === new Date().toDateString()
    ? date.toLocaleTimeString(language ?? [], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString(language ?? [], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** When something happened, with the full time in the tooltip. */
function When({ iso, language }: { iso: string | null } & GateLanguage) {
  if (!iso) return null;
  const date = new Date(iso);
  return (
    <time dateTime={iso} title={date.toLocaleString(language)}>
      {whenText(date, language)}
    </time>
  );
}

/** The chat the gate's messages also go to, and how many could not be sent there; nothing when they go to no chat. */
function GatePosts({ posts, language }: { posts: UserGate["channel_posts"] } & GateLanguage) {
  const text = gatePostsText(posts, language);
  if (!text) return null;
  return (
    <span className="block space-y-0.5 text-xs">
      <span className="block break-words text-muted-foreground">{text.where}</span>
      {text.failure && (
        <span role="status" title={text.error || undefined} className="flex items-start gap-1 text-destructive">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span className="min-w-0 [overflow-wrap:anywhere]">{text.failure}</span>
        </span>
      )}
    </span>
  );
}

/** Where the user wrote a message, in words. */
function viaText(via: string | null, language?: "zh-CN"): string {
  if (!via || via === "dashboard") return language === "zh-CN" ? "任务页面" : "task page";
  if (via === "lark" || via === "lark_bot") return "Lark";
  if (via === "telegram") return "Telegram";
  if (via.startsWith("manager:")) return language === "zh-CN" ? `管家（${via.slice("manager:".length)}）` : `the Manager (${via.slice("manager:".length)})`;
  return via;
}

/** A gate's discussion, oldest first: read when `enabled`, again when `signature` changes, and every few seconds
 * while `follow`. */
function useGateThread(taskId: string, step: string, { enabled, follow, signature }: {
  enabled: boolean;
  follow: boolean;
  signature: string;
}) {
  const [messages, setMessages] = useState<GateMessage[] | null>(null);
  const [error, setError] = useState("");
  // Only the newest request's answer is kept.
  const latest = useRef(0);
  const load = useCallback((signal?: AbortSignal) => {
    const request = ++latest.current;
    return fetchGateThread(taskId, step, signal).then(
      (thread) => {
        if (request !== latest.current) return;
        setMessages(thread.messages);
        setError("");
      },
      (err) => {
        if (request === latest.current && !signal?.aborted) setError(errorText(err));
      },
    );
  }, [taskId, step]);
  // One reader: following polls (its first read is immediate), else the thread is read once per change.
  useEffect(() => {
    if (!enabled) return;
    if (!follow) {
      load();
      return;
    }
    return startPolling(async (signal) => {
      await load(signal);
    }, THREAD_POLL_MS);
  }, [load, enabled, follow, signature]);
  const add = (message: GateMessage) =>
    setMessages((current) => (current && !current.some((m) => m.id === message.id) ? [...current, message] : current));
  return { messages, error, reload: load, add };
}

// The label of a note of Looper's whose text does not say what it is.
const LOOPER_LABELS: Partial<Record<GateMessage["kind"], string>> = {
  updated: "Gate updated",
  reminder: "Reminder",
  closed: "Conclusion",
  error: "Not delivered",
  late: "Sent as a supplement",
};

function MessageRow({ message, language }: { message: GateMessage } & GateLanguage) {
  const chinese = language === "zh-CN";
  if (message.role === "looper") {
    const error = message.kind === "error";
    const Icon = error ? AlertCircle : message.kind === "reminder" ? Bell : Info;
    const label = chinese ? ({ updated: "关卡已更新", reminder: "提醒", closed: "结论", error: "未送达", late: "作为补充发送" } as Partial<Record<GateMessage["kind"], string>>)[message.kind] : LOOPER_LABELS[message.kind];
    return (
      <li
        className={cn(
          "flex gap-2 rounded-md border border-dashed px-3 py-2 text-xs",
          error ? "border-destructive/50 text-destructive" : "border-border text-muted-foreground",
        )}
      >
        <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p>
            <span className="font-medium">Looper</span>
            {label && <span> · {label}</span>} · <When language={language} iso={message.created_at} />
          </p>
          {message.text && <p className="mt-0.5 whitespace-pre-wrap break-words">{message.text}</p>}
        </div>
      </li>
    );
  }
  const mine = message.role === "user";
  return (
    <li className={cn("flex", mine ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "min-w-0 max-w-[85%] rounded-lg border px-3 py-2",
          mine ? "border-primary/30 bg-primary/10" : "border-border bg-muted",
        )}
      >
        <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
          {mine ? <User className="h-3 w-3" aria-hidden="true" /> : <Bot className="h-3 w-3" aria-hidden="true" />}
          <span className="font-medium text-foreground">{mine ? message.author || (chinese ? "你" : "You") : chinese ? "规划助手" : "Planner"}</span>
          {mine && <span>· {viaText(message.via, language)}</span>}
          <span>
            · <When language={language} iso={message.created_at} />
          </span>
        </p>
        {message.kind === "end" && <p className="mt-1 text-xs font-medium">{chinese ? "已结束讨论" : "Ended the discussion"}</p>}
        {message.option && message.agenda_item && (
          <p className="mt-1 text-xs text-muted-foreground">{chinese ? "已选择：" : "Picked for "}{message.agenda_item}</p>
        )}
        {message.text &&
          (mine ? (
            <p className="mt-1 whitespace-pre-wrap break-words text-sm">{message.text}</p>
          ) : (
            <div className="mt-1 break-words">
              <Markdown>{message.text}</Markdown>
            </div>
          ))}
      </div>
    </li>
  );
}

/** The discussion, oldest first, kept scrolled to its newest message. */
function Thread({ messages, error, language }: { messages: GateMessage[] | null; error: string } & GateLanguage) {
  const chinese = language === "zh-CN";
  const list = useRef<HTMLOListElement>(null);
  const count = messages?.length ?? 0;
  useEffect(() => {
    if (list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [count]);
  if (error && !messages) return <p role="alert" className="text-xs text-destructive">{error}</p>;
  if (!messages)
    return <Loader2 className="h-4 w-4 animate-spin text-muted-foreground motion-reduce:animate-none" aria-label={chinese ? "正在加载" : "Loading"} />;
  if (!messages.length) return <p className="text-xs text-muted-foreground">{chinese ? "还没有消息。" : "No messages yet."}</p>;
  return (
    <>
      <ol ref={list} aria-label={chinese ? "讨论" : "Discussion"} className="max-h-96 space-y-2 overflow-y-auto">
        {messages.map((message) => (
          <MessageRow language={language} key={message.id} message={message} />
        ))}
      </ol>
      {error && <p role="alert" className="mt-1 text-xs text-destructive">{error}</p>}
    </>
  );
}

const AGENDA_LOOKS: Record<GateAgendaItem["status"], { label: string; variant: "warning" | "success" | "outline";
                                                      Icon: typeof Circle; tone: string }> = {
  open: { label: "Open", variant: "warning", Icon: Circle, tone: "text-warning" },
  resolved: { label: "Resolved", variant: "success", Icon: CheckCircle2, tone: "text-success" },
  dropped: { label: "Dropped", variant: "outline", Icon: CircleMinus, tone: "text-muted-foreground" },
};

/** One question of the agenda: its state in words, the answer once resolved, and its options while it is open. */
function AgendaRow({
  item,
  idPrefix,
  busy,
  onPick,
  language,
}: {
  item: GateAgendaItem;
  idPrefix: string;
  busy: boolean;
  /** null: the gate takes no message now, so there are no option buttons. */
  onPick: ((option: string) => void) | null;
} & GateLanguage) {
  const chinese = language === "zh-CN";
  const look = AGENDA_LOOKS[item.status] ?? AGENDA_LOOKS.open;
  const questionId = `${idPrefix}-${item.id}`;
  return (
    <li className="flex gap-2">
      <look.Icon className={cn("mt-0.5 h-4 w-4 shrink-0", look.tone)} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="font-mono text-xs text-muted-foreground">{item.id}</span>
          <Badge variant={look.variant}>{chinese ? ({ open: "待答", resolved: "已解决", dropped: "已放弃" } as const)[item.status] : look.label}</Badge>
          <span
            id={questionId}
            className={cn("min-w-0 break-words text-sm", item.status === "dropped" && "text-muted-foreground line-through")}
          >
            {item.question}
          </span>
        </p>
        {item.status === "resolved" && item.answer && (
          <p className="mt-1 break-words text-sm">
            <span className="text-muted-foreground">{chinese ? "回答：" : "Answer:"}</span> {item.answer}
          </p>
        )}
        {item.status === "open" && onPick && item.options.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-2">
            {item.options.map((option) => (
              <Button
                key={option}
                type="button"
                variant="outline"
                aria-describedby={questionId}
                disabled={busy}
                onClick={() => onPick(option)}
                className={TARGET}
              >
                {option}
              </Button>
            ))}
          </div>
        )}
      </div>
    </li>
  );
}

/** A gate that waits for the user: why it stopped, its questions, the discussion and the way to answer or end it.
 * `draft` is the text in its answer box, kept by the caller so that it outlives the gate. */
function WaitingGate({
  task,
  gate,
  draft: text,
  setDraft: setText,
  onChanged,
  language,
}: {
  task: TaskDetail;
  gate: UserGate;
  draft: string;
  setDraft: (text: string) => void;
  onChanged: (gate: UserGate) => void;
} & GateLanguage) {
  const chinese = language === "zh-CN";
  const active = isActiveTask(task);
  // A completed task's gates take no message (409); a stopped, failed or cancelled one keeps them for its restart.
  const takes = task.group !== "completed";
  // The Planner answers: a message of the user's waits for it, or its automatic retry of a failed turn is due; only
  // while the task runs (a stopped one keeps the message for its restart).
  const answering = gateWaitsOn(gate, active) === "planner";
  // Otherwise, the user's messages a failed Planner turn had.
  const notDelivered = answering ? 0 : gate.not_delivered;
  // While it is the user's turn, the thread changes only with the gate, which the task page reads again anyway.
  const thread = useGateThread(task.task_id, gate.step, {
    enabled: true,
    follow: answering,
    signature: JSON.stringify(gate),
  });
  // The task status a message was kept in (its Planner was not running): the note holds until the status changes.
  const [keptIn, setKeptIn] = useState<string | null>(null);
  const kept = keptIn === task.status;
  const { busy, error, run } = useAction();
  const anchor = gateAnchor(gate.step);
  const input = useRef<HTMLTextAreaElement>(null);

  const send = async (post: () => ReturnType<typeof postGateMessage>, clear: boolean) => {
    await run(async () => {
      const posted = await post();
      setKeptIn(posted.delivered === "kept" ? task.status : null);
      thread.add(posted.message);
      if (clear) setText("");
      thread.reload();
      onChanged(posted.gate);
    });
    // The button pressed was disabled while it sent, which drops the focus to the page: back to the answer box.
    input.current?.focus();
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (text.trim()) void send(() => postGateMessage(task.task_id, gate.step, { text }), true);
  };
  const pick = (item: GateAgendaItem) => (option: string) =>
    void send(() => postGateMessage(task.task_id, gate.step, { text: option, agenda_item: item.id, option }), false);

  return (
    <section
      id={anchor}
      tabIndex={-1}
      aria-labelledby={`${anchor}-title`}
      className="rounded-lg border-2 border-warning/60 bg-card focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <div className="space-y-1 border-b border-border px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <MessageCircleQuestionMark className="h-4 w-4 text-warning" aria-hidden="true" />
          <span className="font-mono text-muted-foreground">{chinese ? "关卡" : "gate"} {gate.step}</span>
          {gate.opened_at && (
            <span className="text-muted-foreground">
              {chinese ? "· 已开启 " : "· open for "}{sinceText(gate.opened_at, language)}{chinese ? "（自 " : " (since "}<When language={language} iso={gate.opened_at} />{chinese ? "）" : ")"}
            </span>
          )}
          {/* Whose turn it is: the one place the panel says so. */}
          <span
            role="status"
            className={
              notDelivered > 0 && !answering
                ? "flex basis-full flex-wrap items-center gap-x-2 gap-y-1 text-destructive"
                : "ml-auto"
            }
          >
            {answering ? (
              <span className="flex items-center gap-1.5 text-primary">
                <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                {chinese ? "规划助手回复中" : "The Planner is answering"}
              </span>
            ) : notDelivered > 0 ? (
              <>
                <Badge variant="destructive">{chinese ? "未送达" : "Not delivered"}</Badge>
                <span>
                  {chinese ? `规划助手未能接收你最近的 ${notDelivered} 条消息。` : notDelivered === 1
                    ? "The Planner could not take your last message."
                    : `The Planner could not take your last ${notDelivered} messages.`}{" "}
                  {gate.retry_at && active ? (
                    <>
                      {chinese ? "将在 " : "It tries again at "}<When language={language} iso={gate.retry_at} />{chinese ? " 重试；你下次发送消息时也会一并重发。" : `, and your next message sends ${notDelivered === 1 ? "it" : "them"} too.`}
                    </>
                  ) : (
                    chinese ? "你下次发送消息时会一并重发。" : `Your next message sends ${notDelivered === 1 ? "it" : "them"} again.`
                  )}
                </span>
              </>
            ) : (
              <Badge variant="warning">{chinese ? "等你回复" : "Your turn"}</Badge>
            )}
          </span>
        </div>
        <GatePosts language={language} posts={gate.channel_posts} />
        <p className="pt-1 text-xs font-medium text-muted-foreground">{chinese ? "暂停原因" : "Why it stopped"}</p>
        <h2 id={`${anchor}-title`} className="break-words text-base font-semibold">
          {gate.topic}
        </h2>
        {gate.findings && <p className="whitespace-pre-wrap break-words text-sm">{gate.findings}</p>}
      </div>

      <div className="space-y-4 px-4 py-3">
        {gate.agenda.length > 0 && (
          <div>
            <h3 className="mb-2 text-xs font-medium text-muted-foreground">{chinese ? "待答问题" : "Questions"}</h3>
            <ol className="space-y-3">
              {gate.agenda.map((item) => (
                <AgendaRow language={language} key={item.id} item={item} idPrefix={anchor} busy={busy} onPick={takes ? pick(item) : null} />
              ))}
            </ol>
          </div>
        )}

        <div>
          <h3 className="mb-2 text-xs font-medium text-muted-foreground">{chinese ? "讨论" : "Discussion"}</h3>
          <Thread language={language} messages={thread.messages} error={thread.error} />
        </div>

        {takes ? (
          <form onSubmit={submit} className="space-y-2">
            <label htmlFor={`${anchor}-text`} className="sr-only">
              {chinese ? "你的回答" : "Your answer"}
            </label>
            <textarea
              ref={input}
              id={`${anchor}-text`}
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              maxLength={GATE_MESSAGE_MAX_CHARS}
              placeholder={chinese ? "回答、追问，或补充规划助手需要知道的信息" : "Answer, ask back, or add what the Planner should know"}
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            {(!active || kept) && (
              <p role="status" className="text-xs text-muted-foreground">
                {chinese ? (kept ? "消息已暂存：规划助手未运行，任务继续时会收到消息（重新开始会丢弃消息）。" : "任务未运行：发送的消息会暂存，任务继续时交给规划助手（重新开始会丢弃消息）。") : kept
                  ? "Your message is kept: the task's Planner is not running, and gets it when the task continues (Start over discards it)."
                  : "The task is not running: what you send is kept, and its Planner gets it when the task continues (Start over discards it)."}
              </p>
            )}
            {error && (
              <p role="alert" className="text-xs text-destructive">
                {error}
              </p>
            )}
            <div className="flex flex-wrap items-start gap-2">
              <Button type="submit" disabled={busy || !text.trim()} className={TARGET}>
                {busy ? (
                  <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                ) : (
                  <Send className="h-4 w-4" aria-hidden="true" />
                )}
                {chinese ? "发送" : "Send"}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                aria-describedby={`${anchor}-end-help`}
                onClick={() => void send(() => endGateDiscussion(task.task_id, gate.step, text), true)}
                className={TARGET}
              >
                {chinese ? "结束讨论，按当前结论继续" : "End discussion, go on with current conclusions"}
              </Button>
            </div>
            <p id={`${anchor}-end-help`} className="text-xs text-muted-foreground">
              {chinese ? "规划助手会按已有结论关闭关卡，并说明对未解决问题的假设。输入框中的文字会一同发送。" : <>The Planner then closes the gate with what is settled so far, and states what it assumes for the questions
              still open. Text in the box goes with it.</>}
            </p>
          </form>
        ) : (
          <p className="text-xs text-muted-foreground">{chinese ? "任务已完成：此关卡不再接受消息。" : "The task has completed: this gate takes no more messages."}</p>
        )}
      </div>
    </section>
  );
}

/** What a gate that passed settled, and how the plan changed with it. */
function Conclusion({ gate, language }: { gate: UserGate } & GateLanguage) {
  const chinese = language === "zh-CN";
  const summary = gate.summary ?? { decisions: [], assumptions: [], open: [] };
  const lists: [string, string[], boolean][] = [
    [chinese ? "决定" : "Decisions", summary.decisions, false],
    [chinese ? "假设" : "Assumptions", summary.assumptions, true],
    [chinese ? "尚未解决" : "Still open", summary.open, false],
  ];
  const shown = lists.filter(([, items]) => items.length > 0);
  const change = gate.plan_change;
  const changes: [string, string[]][] = change
    ? ([[chinese ? "已添加" : "Added", change.added], [chinese ? "已移除" : "Dropped", change.dropped], [chinese ? "已停止" : "Stopped", change.stopped]] as [string, string[]][])
        .filter(([, steps]) => steps.length > 0)
    : [];
  return (
    <div className="space-y-3 text-sm">
      {shown.length === 0 && <p>{chinese ? "无需补充：计划继续按原定安排进行。" : "Nothing to note: the plan goes on as planned."}</p>}
      {shown.map(([title, items, unconfirmed]) => (
        <div key={title}>
          <p className="flex flex-wrap items-center gap-2 text-xs font-medium text-muted-foreground">
            {title}
            {unconfirmed && <Badge variant="warning">{chinese ? "尚未经你确认" : "not confirmed by you"}</Badge>}
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {items.map((item, i) => (
              <li key={i} className="break-words">
                {item}
              </li>
            ))}
          </ul>
        </div>
      ))}
      <p className="text-xs">
        <span className="font-medium text-muted-foreground">{chinese ? "计划变更" : "Plan change"}</span>{" "}
        {changes.length === 0
          ? (chinese ? "无" : "none")
          : changes.map(([label, steps], i) => (
              <span key={label}>
                {i > 0 && " · "}
                {label} <span className="font-mono">{steps.join(", ")}</span>
              </span>
            ))}
      </p>
    </div>
  );
}

/** A gate that closed, collapsed to its line unless `open`: its conclusion (passed) or why it was cancelled, and its
 * discussion, read when first shown; below it, a `draft` the user had not sent when it closed. */
function ClosedGate({
  taskId,
  gate,
  open,
  draft,
  onDiscard,
  language,
}: {
  taskId: string;
  gate: UserGate;
  open: boolean;
  draft: string;
  onDiscard: () => void;
} & GateLanguage) {
  const chinese = language === "zh-CN";
  const passed = gate.state === "passed";
  const [showThread, setShowThread] = useState(false);
  const thread = useGateThread(taskId, gate.step, { enabled: showThread, follow: false, signature: gate.state });
  return (
    <li>
      <details id={gateAnchor(gate.step)} open={open} className="group/gate">
        <summary className="flex min-h-11 cursor-pointer list-none items-start gap-2 px-4 py-3 hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
          <ChevronDown
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 shrink-0 -rotate-90 text-muted-foreground transition-transform duration-150 group-open/gate:rotate-0 motion-reduce:transition-none"
          />
          {passed ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" aria-hidden="true" />
          ) : (
            <CircleX className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
          )}
          <span className="min-w-0 flex-1 space-y-0.5">
            <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="font-mono text-xs text-muted-foreground">{chinese ? "关卡" : "gate"} {gate.step}</span>
              <GateBadge language={language} gate={gate} active={false} />
              <span className="min-w-0 break-words text-sm font-medium">{gate.topic}</span>
            </span>
            <span className="block text-xs text-muted-foreground">
              {gate.closed_at && (
                <>
                  {chinese ? "关闭于 " : "Closed "}<When language={language} iso={gate.closed_at} />
                </>
              )}
              {gate.ended_by_user && (chinese ? " · 你已结束讨论" : " · you ended the discussion")}
            </span>
            <GatePosts language={language} posts={gate.channel_posts} />
          </span>
        </summary>
        <div className="space-y-3 border-t border-border py-3 pl-10 pr-4">
          {passed ? (
            <Conclusion language={language} gate={gate} />
          ) : (
            <p className="break-words text-sm">
              <span className="text-muted-foreground">{chinese ? "原因：" : "Why:"}</span> {gate.resolution || (chinese ? "未提供原因" : "no reason given")}
              {!gate.opened_at && <span className="text-muted-foreground">{chinese ? "（开启前已取消；未向你提问）" : " (cancelled before it opened; you were not asked)"}</span>}
            </p>
          )}
          <details className="group/thread" onToggle={(e) => e.currentTarget.open && setShowThread(true)}>
            <summary className="flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
              <ChevronDown
                aria-hidden="true"
                className="h-3.5 w-3.5 -rotate-90 transition-transform duration-150 group-open/thread:rotate-0 motion-reduce:transition-none"
              />
              {chinese ? "讨论" : "Discussion"}
            </summary>
            <div className="pt-1">
              <Thread language={language} messages={thread.messages} error={thread.error} />
            </div>
          </details>
        </div>
      </details>
      {draft.trim() && (
        <div className="pb-3 pl-10 pr-4">
          <div className="space-y-2 rounded-md border border-dashed border-border px-3 py-2">
            <p className="text-xs font-medium text-warning">{chinese ? "未发送：发送前关卡已关闭。" : "Not sent: the gate closed before you sent this."}</p>
            <p className="whitespace-pre-wrap break-words text-sm">{draft}</p>
            <Button type="button" variant="outline" size="sm" onClick={onDiscard} className={TARGET}>
              {chinese ? "丢弃" : "Discard"}
            </Button>
          </div>
        </div>
      )}
    </li>
  );
}

/**
 * The task's user gates, at the top of its Overview: each gate that waits for the user, then the gates that closed,
 * oldest first and collapsed, but for the latest while none waits. Nothing while no gate has opened. `onChanged`
 * gets a gate as the server answered a message in it.
 */
export function TaskGates({ task, onChanged, language }: { task: TaskDetail; onChanged: (gate: UserGate) => void } & GateLanguage) {
  // The text in each gate's answer box, by its step: kept here, so that a gate that closes before it is sent still
  // shows it.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const setDraft = (step: string) => (text: string) => setDrafts((all) => ({ ...all, [step]: text }));
  const gates = (task.gates ?? []).filter(isGateShown);
  if (gates.length === 0) return null;
  const waiting = gates.filter(isGateWaiting);
  const closed = gates
    .filter((gate) => !isGateWaiting(gate))
    .sort((a, b) => Date.parse(a.closed_at ?? "") - Date.parse(b.closed_at ?? "") || 0);
  const latest = waiting.length === 0 ? closed[closed.length - 1] : undefined;
  return (
    <div className="space-y-3">
      {waiting.map((gate) => (
        <WaitingGate
          language={language}
          key={gate.step_id}
          task={task}
          gate={gate}
          draft={drafts[gate.step] ?? ""}
          setDraft={setDraft(gate.step)}
          onChanged={onChanged}
        />
      ))}
      {closed.length > 0 && (
        <section aria-label={language === "zh-CN" ? "关卡" : "Gates"}>
          <ol className="divide-y divide-border rounded-lg border border-border bg-card">
            {closed.map((gate) => (
              <ClosedGate
                language={language}
                key={gate.step_id}
                taskId={task.task_id}
                gate={gate}
                open={gate === latest}
                draft={drafts[gate.step] ?? ""}
                onDiscard={() => setDraft(gate.step)("")}
              />
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}
