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
import { cn } from "@/lib/utils";

/** How often a gate that waits reads its discussion again while its task runs. */
const THREAD_POLL_MS = 3000;

// Big enough to hit (44 px), wrapping a long label instead of widening the page (also within a word with no spaces),
// with a focus ring that shows.
const TARGET = "h-auto min-h-11 max-w-full whitespace-normal [overflow-wrap:anywhere] py-2 text-left focus-visible:ring-2";

/** A gate as a badge: its state's word and tone, or whose turn it is while it is discussed (the Planner's only while
 * its task runs, `active`). */
export function GateBadge({ gate, active }: { gate: Pick<UserGate, "state" | "waiting_on">; active: boolean }) {
  const look = gateLook(gate, active);
  return <Badge variant={look.variant}>{look.label}</Badge>;
}

/** The time of day for today, else the date and time. */
function whenText(date: Date): string {
  return date.toDateString() === new Date().toDateString()
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** When something happened, with the full time in the tooltip. */
function When({ iso }: { iso: string | null }) {
  if (!iso) return null;
  const date = new Date(iso);
  return (
    <time dateTime={iso} title={date.toLocaleString()}>
      {whenText(date)}
    </time>
  );
}

/** The chat the gate's messages also go to, and how many could not be sent there; nothing when they go to no chat. */
function GatePosts({ posts }: { posts: UserGate["channel_posts"] }) {
  const text = gatePostsText(posts);
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
function viaText(via: string | null): string {
  if (!via || via === "dashboard") return "task page";
  if (via === "lark") return "Lark";
  if (via === "telegram") return "Telegram";
  if (via.startsWith("manager:")) return `the Manager (${via.slice("manager:".length)})`;
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
  const load = useCallback(() => {
    const request = ++latest.current;
    fetchGateThread(taskId, step).then(
      (thread) => {
        if (request !== latest.current) return;
        setMessages(thread.messages);
        setError("");
      },
      (err) => {
        if (request === latest.current) setError(errorText(err));
      },
    );
  }, [taskId, step]);
  useEffect(() => {
    if (enabled) load();
  }, [load, enabled, signature]);
  useEffect(() => {
    if (!enabled || !follow) return;
    const timer = setInterval(load, THREAD_POLL_MS);
    return () => clearInterval(timer);
  }, [load, enabled, follow]);
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

function MessageRow({ message }: { message: GateMessage }) {
  if (message.role === "looper") {
    const error = message.kind === "error";
    const Icon = error ? AlertCircle : message.kind === "reminder" ? Bell : Info;
    const label = LOOPER_LABELS[message.kind];
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
            {label && <span> · {label}</span>} · <When iso={message.created_at} />
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
          <span className="font-medium text-foreground">{mine ? message.author || "You" : "Planner"}</span>
          {mine && <span>· {viaText(message.via)}</span>}
          <span>
            · <When iso={message.created_at} />
          </span>
        </p>
        {message.kind === "end" && <p className="mt-1 text-xs font-medium">Ended the discussion</p>}
        {message.option && message.agenda_item && (
          <p className="mt-1 text-xs text-muted-foreground">Picked for {message.agenda_item}</p>
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
function Thread({ messages, error }: { messages: GateMessage[] | null; error: string }) {
  const list = useRef<HTMLOListElement>(null);
  const count = messages?.length ?? 0;
  useEffect(() => {
    if (list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [count]);
  if (error && !messages) return <p role="alert" className="text-xs text-destructive">{error}</p>;
  if (!messages)
    return <Loader2 className="h-4 w-4 animate-spin text-muted-foreground motion-reduce:animate-none" aria-label="Loading" />;
  if (!messages.length) return <p className="text-xs text-muted-foreground">No messages yet.</p>;
  return (
    <>
      <ol ref={list} aria-label="Discussion" className="max-h-96 space-y-2 overflow-y-auto">
        {messages.map((message) => (
          <MessageRow key={message.id} message={message} />
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
}: {
  item: GateAgendaItem;
  idPrefix: string;
  busy: boolean;
  /** null: the gate takes no message now, so there are no option buttons. */
  onPick: ((option: string) => void) | null;
}) {
  const look = AGENDA_LOOKS[item.status] ?? AGENDA_LOOKS.open;
  const questionId = `${idPrefix}-${item.id}`;
  return (
    <li className="flex gap-2">
      <look.Icon className={cn("mt-0.5 h-4 w-4 shrink-0", look.tone)} aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span className="font-mono text-xs text-muted-foreground">{item.id}</span>
          <Badge variant={look.variant}>{look.label}</Badge>
          <span
            id={questionId}
            className={cn("min-w-0 break-words text-sm", item.status === "dropped" && "text-muted-foreground line-through")}
          >
            {item.question}
          </span>
        </p>
        {item.status === "resolved" && item.answer && (
          <p className="mt-1 break-words text-sm">
            <span className="text-muted-foreground">Answer:</span> {item.answer}
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
}: {
  task: TaskDetail;
  gate: UserGate;
  draft: string;
  setDraft: (text: string) => void;
  onChanged: (gate: UserGate) => void;
}) {
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
          <span className="font-mono text-muted-foreground">gate {gate.step}</span>
          {gate.opened_at && (
            <span className="text-muted-foreground">
              · open for {sinceText(gate.opened_at)} (since <When iso={gate.opened_at} />)
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
                The Planner is answering
              </span>
            ) : notDelivered > 0 ? (
              <>
                <Badge variant="destructive">Not delivered</Badge>
                <span>
                  {notDelivered === 1
                    ? "The Planner could not take your last message."
                    : `The Planner could not take your last ${notDelivered} messages.`}{" "}
                  {gate.retry_at && active ? (
                    <>
                      It tries again at <When iso={gate.retry_at} />, and your next message sends{" "}
                      {notDelivered === 1 ? "it" : "them"} too.
                    </>
                  ) : (
                    `Your next message sends ${notDelivered === 1 ? "it" : "them"} again.`
                  )}
                </span>
              </>
            ) : (
              <Badge variant="warning">Your turn</Badge>
            )}
          </span>
        </div>
        <GatePosts posts={gate.channel_posts} />
        <p className="pt-1 text-xs font-medium text-muted-foreground">Why it stopped</p>
        <h2 id={`${anchor}-title`} className="break-words text-base font-semibold">
          {gate.topic}
        </h2>
        {gate.findings && <p className="whitespace-pre-wrap break-words text-sm">{gate.findings}</p>}
      </div>

      <div className="space-y-4 px-4 py-3">
        {gate.agenda.length > 0 && (
          <div>
            <h3 className="mb-2 text-xs font-medium text-muted-foreground">Questions</h3>
            <ol className="space-y-3">
              {gate.agenda.map((item) => (
                <AgendaRow key={item.id} item={item} idPrefix={anchor} busy={busy} onPick={takes ? pick(item) : null} />
              ))}
            </ol>
          </div>
        )}

        <div>
          <h3 className="mb-2 text-xs font-medium text-muted-foreground">Discussion</h3>
          <Thread messages={thread.messages} error={thread.error} />
        </div>

        {takes ? (
          <form onSubmit={submit} className="space-y-2">
            <label htmlFor={`${anchor}-text`} className="sr-only">
              Your answer
            </label>
            <textarea
              ref={input}
              id={`${anchor}-text`}
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={3}
              maxLength={GATE_MESSAGE_MAX_CHARS}
              placeholder="Answer, ask back, or add what the Planner should know"
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            />
            {(!active || kept) && (
              <p role="status" className="text-xs text-muted-foreground">
                {kept
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
                Send
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={busy}
                aria-describedby={`${anchor}-end-help`}
                onClick={() => void send(() => endGateDiscussion(task.task_id, gate.step, text), true)}
                className={TARGET}
              >
                End discussion, go on with current conclusions
              </Button>
            </div>
            <p id={`${anchor}-end-help`} className="text-xs text-muted-foreground">
              The Planner then closes the gate with what is settled so far, and states what it assumes for the questions
              still open. Text in the box goes with it.
            </p>
          </form>
        ) : (
          <p className="text-xs text-muted-foreground">The task has completed: this gate takes no more messages.</p>
        )}
      </div>
    </section>
  );
}

/** What a gate that passed settled, and how the plan changed with it. */
function Conclusion({ gate }: { gate: UserGate }) {
  const summary = gate.summary ?? { decisions: [], assumptions: [], open: [] };
  const lists: [string, string[], boolean][] = [
    ["Decisions", summary.decisions, false],
    ["Assumptions", summary.assumptions, true],
    ["Still open", summary.open, false],
  ];
  const shown = lists.filter(([, items]) => items.length > 0);
  const change = gate.plan_change;
  const changes: [string, string[]][] = change
    ? ([["Added", change.added], ["Dropped", change.dropped], ["Stopped", change.stopped]] as [string, string[]][])
        .filter(([, steps]) => steps.length > 0)
    : [];
  return (
    <div className="space-y-3 text-sm">
      {shown.length === 0 && <p>Nothing to note: the plan goes on as planned.</p>}
      {shown.map(([title, items, unconfirmed]) => (
        <div key={title}>
          <p className="flex flex-wrap items-center gap-2 text-xs font-medium text-muted-foreground">
            {title}
            {unconfirmed && <Badge variant="warning">not confirmed by you</Badge>}
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
        <span className="font-medium text-muted-foreground">Plan change</span>{" "}
        {changes.length === 0
          ? "none"
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
}: {
  taskId: string;
  gate: UserGate;
  open: boolean;
  draft: string;
  onDiscard: () => void;
}) {
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
              <span className="font-mono text-xs text-muted-foreground">gate {gate.step}</span>
              <GateBadge gate={gate} active={false} />
              <span className="min-w-0 break-words text-sm font-medium">{gate.topic}</span>
            </span>
            <span className="block text-xs text-muted-foreground">
              {gate.closed_at && (
                <>
                  Closed <When iso={gate.closed_at} />
                </>
              )}
              {gate.ended_by_user && " · you ended the discussion"}
            </span>
            <GatePosts posts={gate.channel_posts} />
          </span>
        </summary>
        <div className="space-y-3 border-t border-border py-3 pl-10 pr-4">
          {passed ? (
            <Conclusion gate={gate} />
          ) : (
            <p className="break-words text-sm">
              <span className="text-muted-foreground">Why:</span> {gate.resolution || "no reason given"}
              {!gate.opened_at && <span className="text-muted-foreground"> (cancelled before it opened; you were not asked)</span>}
            </p>
          )}
          <details className="group/thread" onToggle={(e) => e.currentTarget.open && setShowThread(true)}>
            <summary className="flex min-h-11 cursor-pointer list-none items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
              <ChevronDown
                aria-hidden="true"
                className="h-3.5 w-3.5 -rotate-90 transition-transform duration-150 group-open/thread:rotate-0 motion-reduce:transition-none"
              />
              Discussion
            </summary>
            <div className="pt-1">
              <Thread messages={thread.messages} error={thread.error} />
            </div>
          </details>
        </div>
      </details>
      {draft.trim() && (
        <div className="pb-3 pl-10 pr-4">
          <div className="space-y-2 rounded-md border border-dashed border-border px-3 py-2">
            <p className="text-xs font-medium text-warning">Not sent: the gate closed before you sent this.</p>
            <p className="whitespace-pre-wrap break-words text-sm">{draft}</p>
            <Button type="button" variant="outline" size="sm" onClick={onDiscard} className={TARGET}>
              Discard
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
export function TaskGates({ task, onChanged }: { task: TaskDetail; onChanged: (gate: UserGate) => void }) {
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
          key={gate.step_id}
          task={task}
          gate={gate}
          draft={drafts[gate.step] ?? ""}
          setDraft={setDraft(gate.step)}
          onChanged={onChanged}
        />
      ))}
      {closed.length > 0 && (
        <section aria-label="Gates">
          <ol className="divide-y divide-border rounded-lg border border-border bg-card">
            {closed.map((gate) => (
              <ClosedGate
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
