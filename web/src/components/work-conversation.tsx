import { useEffect, useRef, useState } from "react";
import { ChatComposer } from "@/components/chat-composer";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import {
  fetchChatMessages,
  fetchChatTargets,
  type ChatTarget,
  type DbChatMessage,
  type SSEEvent,
} from "@/lib/api";
import { historyFrom, joinStaged, pullBack } from "@/lib/composer";
import { randomId } from "@/lib/ids";
import { useReplyStream } from "@/lib/use-reply-stream";
import { errorText } from "@/lib/use-action";
import { cn } from "@/lib/utils";

/** Who the pane is talking as. Manager stays a link until its own adapter exists. */
export type WorkIdentity = { kind: "manager" } | { kind: "planner"; taskId: string };

export interface WorkPaneMessage {
  id: string;
  role: string;
  content: string;
  streaming?: boolean;
  outcome?: string;
}

/** The task row the page already loaded. Availability does not read `group`. */
export interface WorkConversationDetail {
  title?: string;
  status?: string;
  project_name?: string;
  group?: string;
}

export interface WorkConversationProps {
  identity: WorkIdentity;
  missing?: boolean;
  canSend: boolean;
  refusal?: string;
  detail?: WorkConversationDetail | null;
  messages: WorkPaneMessage[];
  events?: SSEEvent[];
  history: "loading" | "empty" | "ready" | "error";
  historyError?: string;
  onRetry?: () => void;
  phase?: string;
  replying?: boolean;
  draft: string;
  onDraft: (draft: string) => void;
  staged: string[];
  onStaged: (staged: string[]) => void;
  inputHistory?: string[];
  onSend: (text: string) => void;
  onStop?: () => void;
  notice?: string;
  canStop?: boolean;
}

const RECENT = "仅显示最近的对话";
const EMPTY = "还没有对话";
const LOADING = "正在加载对话…";
const READ_FAILED = "对话读取失败";
const NOT_FOUND = "任务不存在";
const CHECKING = "Checking chat availability";
const CHECK_FAILED = "Chat availability could not be checked";
const NO_PLANNER = "Only tasks with a running Planner can be chatted with";
const PLANNER_ENDED = "This task has ended, so its Planner no longer takes messages.";
const CONNECT_FAILED = "Failed to connect. Is the server running?";
const STREAM_LOST = "回复流已断开，服务端可能仍在运行";

/**
 * Draft and queue for each task. The page remounts the pane with `key={taskId}`,
 * and a disabled or full sessionStorage keeps only this copy.
 */
type ComposerSlot = { draft: string; staged: string[] };
const composerMemory = new Map<string, ComposerSlot>();

export function clearWorkComposerMemory(): void {
  composerMemory.clear();
}

function draftKey(taskId: string): string {
  return `looper-work-draft:planner-${taskId}`;
}

function stagedKey(taskId: string): string {
  return `looper-work-staged:planner-${taskId}`;
}

function storageDraft(taskId: string): string {
  try {
    return sessionStorage.getItem(draftKey(taskId)) ?? "";
  } catch {
    return "";
  }
}

function storageStaged(taskId: string): string[] {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(stagedKey(taskId)) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function slot(taskId: string): ComposerSlot {
  const existing = composerMemory.get(taskId);
  if (existing) return existing;
  const created = { draft: storageDraft(taskId), staged: storageStaged(taskId) };
  composerMemory.set(taskId, created);
  return created;
}

function readStoredDraft(taskId: string): string {
  return slot(taskId).draft;
}

function readStoredStaged(taskId: string): string[] {
  return [...slot(taskId).staged];
}

function writeStoredDraft(taskId: string, draft: string) {
  slot(taskId).draft = draft;
  try {
    if (draft) sessionStorage.setItem(draftKey(taskId), draft);
    else sessionStorage.removeItem(draftKey(taskId));
  } catch {
    // Disabled or full: the memory copy is the copy that survives a task switch.
  }
}

function writeStoredStaged(taskId: string, staged: string[]) {
  slot(taskId).staged = [...staged];
  try {
    if (staged.length === 0) sessionStorage.removeItem(stagedKey(taskId));
    else sessionStorage.setItem(stagedKey(taskId), JSON.stringify(staged));
  } catch {
    // Disabled or full: the memory copy is the copy that survives a task switch.
  }
}

function fromDb(rows: DbChatMessage[]): WorkPaneMessage[] {
  return rows.map((row) => ({
    id: row.message_id,
    role: row.role,
    content: row.content,
  }));
}

/** Fold a turn's stream onto messages. Unknown kinds are not messages. */
function foldStream(messages: WorkPaneMessage[], events: SSEEvent[]): { messages: WorkPaneMessage[]; phase: string } {
  const next = messages.map((message) => ({ ...message }));
  let phase = "";
  const streaming = () => {
    let row = [...next].reverse().find((message) => message.role === "assistant" && message.streaming);
    if (!row) {
      row = { id: "stream", role: "assistant", content: "", streaming: true };
      next.push(row);
    }
    return row;
  };
  for (const event of events) {
    if (event.kind === "answer.delta") {
      const row = streaming();
      row.content += String(event.payload.text ?? "");
    } else if (event.kind === "answer.final") {
      const row = streaming();
      const text = String(event.payload.text ?? "");
      row.content = text || row.content || "Done.";
      row.streaming = false;
      row.outcome = event.payload.outcome === "stopped" ? "stopped" : undefined;
      phase = "";
    } else if (event.kind === "agent.phase") {
      phase = String(event.payload.label ?? "");
    }
  }
  return { messages: next, phase };
}

function sendFailure(error: unknown): string {
  if (error instanceof TypeError || !(error instanceof Error)) return CONNECT_FAILED;
  return error.message;
}

function MessageList({ messages }: { messages: WorkPaneMessage[] }) {
  return (
    <>
      {messages.map((message) => {
        const known = message.role === "user" || message.role === "assistant";
        return (
          <div
            key={message.id}
            data-message-id={message.id}
            data-role={message.role}
            className={cn("flex", message.role === "user" ? "justify-end" : "justify-start")}
          >
            <div
              className={cn(
                "min-w-0 max-w-[85%] rounded-lg px-4 py-2.5 text-sm [overflow-wrap:anywhere]",
                message.role === "user" ? "border border-border bg-card text-card-foreground"
                  : known ? "bg-muted text-foreground"
                    : "text-muted-foreground",
              )}
            >
              {message.role === "assistant" ? (
                <Markdown>{message.content}</Markdown>
              ) : (
                <p className="whitespace-pre-wrap">{message.content}</p>
              )}
              {message.outcome === "stopped" && (
                <p className="mt-1.5 text-xs text-muted-foreground">Stopped</p>
              )}
            </div>
          </div>
        );
      })}
    </>
  );
}

/**
 * The Work centre pane. An adapter supplies identity, whether sending is allowed,
 * the shared task, stream events and the send/stop callbacks. Manager renders nothing:
 * the page keeps the Chat link.
 */
export function WorkConversation(props: WorkConversationProps) {
  const {
    identity, missing, canSend, refusal, detail, messages, events, history, historyError, onRetry,
    phase, replying, draft, onDraft, staged, onStaged, inputHistory, onSend, onStop, notice, canStop,
  } = props;
  if (identity.kind === "manager") return null;
  if (missing) return <p role="status" data-identity={`planner:${identity.taskId}`}>{NOT_FOUND}</p>;

  const folded = foldStream(messages, events ?? []);
  const shownPhase = phase || folded.phase;
  const shown = folded.messages;
  const loaded = history === "ready" || history === "empty";

  return (
    <section
      aria-label="Conversation"
      data-identity={`planner:${identity.taskId}`}
      data-task-status={detail?.status ?? ""}
      className="flex h-full min-h-0 flex-1 flex-col"
    >
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
        {history === "loading" && shown.length === 0 && <p role="status">{LOADING}</p>}
        {history === "error" && (
          <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
            <span>{READ_FAILED} {historyError}</span>
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>重试</Button>
          </p>
        )}
        {loaded && <p className="text-center text-xs text-muted-foreground">{RECENT}</p>}
        {loaded && shown.length === 0 && <p role="status">{EMPTY}</p>}
        {(history !== "error" || shown.length > 0) && <MessageList messages={shown} />}
      </div>
      <div className="shrink-0 space-y-2 border-t border-border p-4">
        {refusal && <p role="status" className="text-xs text-muted-foreground">{refusal}</p>}
        {shownPhase && <p role="status" data-phase={shownPhase}>{shownPhase}</p>}
        <ChatComposer
          key={identity.taskId}
          draft={draft}
          onDraft={onDraft}
          staged={staged}
          onStaged={onStaged}
          history={inputHistory ?? historyFrom(shown)}
          replying={replying ?? false}
          canStop={canStop ?? false}
          onSend={onSend}
          onStop={onStop ?? (() => {})}
          notice={notice}
          disabled={!canSend}
          controls="neutral"
        />
      </div>
    </section>
  );
}

/**
 * Planner adapter for one selected task. The thread is `planner-<task id>` and the
 * send target is `{kind:'planner', task_id}`. There is no Stop and no turn poll.
 */
export function PlannerWorkConversation({
  taskId,
  detail = null,
  missing = false,
}: {
  taskId: string;
  detail?: WorkConversationDetail | null;
  missing?: boolean;
}) {
  const identity: WorkIdentity = { kind: "planner", taskId };
  const taskRef = useRef(taskId);
  taskRef.current = taskId;
  const gen = useRef(0);
  const genTask = useRef(taskId);
  if (genTask.current !== taskId) {
    genTask.current = taskId;
    gen.current += 1;
  }
  const reply = useReplyStream(() => ({ id: taskRef.current, threadId: `planner-${taskRef.current}` }));
  const replyRef = useRef(reply);
  replyRef.current = reply;

  const [draft, setDraftState] = useState(() => (missing ? "" : readStoredDraft(taskId)));
  const [staged, setStagedState] = useState<string[]>(() => (missing ? [] : readStoredStaged(taskId)));
  const draftRef = useRef(draft);
  const stagedRef = useRef(staged);
  const storedTask = useRef(taskId);
  let shownDraft = draft;
  let shownStaged = staged;
  if (storedTask.current !== taskId) {
    storedTask.current = taskId;
    shownDraft = missing ? "" : readStoredDraft(taskId);
    shownStaged = missing ? [] : readStoredStaged(taskId);
    draftRef.current = shownDraft;
    stagedRef.current = shownStaged;
    setDraftState(shownDraft);
    setStagedState(shownStaged);
  }

  const setDraft = (next: string | ((value: string) => string)) => {
    const value = typeof next === "function" ? next(draftRef.current) : next;
    draftRef.current = value;
    writeStoredDraft(taskRef.current, value);
    setDraftState(value);
  };
  const setStaged = (next: string[]) => {
    stagedRef.current = next;
    writeStoredStaged(taskRef.current, next);
    setStagedState(next);
  };

  const [live, setLive] = useState({
    taskId,
    messages: [] as WorkPaneMessage[],
    history: (missing ? "empty" : "loading") as WorkConversationProps["history"],
    historyError: "",
    phase: "",
    notice: "",
    replying: false,
  });
  if (live.taskId !== taskId) {
    setLive({
      taskId,
      messages: [],
      history: missing ? "empty" : "loading",
      historyError: "",
      phase: "",
      notice: "",
      replying: false,
    });
  }
  const liveRef = useRef(live);
  liveRef.current = live.taskId === taskId ? live : {
    taskId, messages: [], history: "loading" as const, historyError: "", phase: "", notice: "", replying: false,
  };

  const [availability, setAvailability] = useState({ canSend: false, reason: missing ? "" : CHECKING });
  const availTask = useRef(taskId);
  let availabilityView = availability;
  if (availTask.current !== taskId) {
    availTask.current = taskId;
    availabilityView = { canSend: false, reason: missing ? "" : CHECKING };
    setAvailability(availabilityView);
  }
  const canSendRef = useRef(false);
  canSendRef.current = availabilityView.canSend && !missing;
  const [retry, setRetry] = useState(0);
  const sendGen = useRef(0);
  const finalSeen = useRef(false);
  const sendRef = useRef<(text: string) => void>(() => {});

  useEffect(() => () => { replyRef.current.cancel(); }, [taskId]);

  useEffect(() => {
    if (missing) return;
    const ctrl = new AbortController();
    const turn = gen.current;
    setAvailability({ canSend: false, reason: CHECKING });
    fetchChatTargets(ctrl.signal).then((targets) => {
      if (ctrl.signal.aborted || turn !== gen.current || taskRef.current !== taskId) return;
      const planner = targets.find((target) => target.kind === "planner" && target.task_id === taskId);
      if (!planner) {
        setAvailability({ canSend: false, reason: NO_PLANNER });
        return;
      }
      setAvailability({
        canSend: planner.can_send,
        reason: planner.can_send ? "" : PLANNER_ENDED,
      });
    }).catch((error: unknown) => {
      if (ctrl.signal.aborted || turn !== gen.current || (error instanceof Error && error.name === "AbortError")) return;
      const reason = errorText(error).trim();
      setAvailability({ canSend: false, reason: reason || CHECK_FAILED });
    });
    return () => ctrl.abort();
  }, [taskId, missing]);

  useEffect(() => {
    if (missing) return;
    const ctrl = new AbortController();
    const turn = gen.current;
    const sent = sendGen.current;
    setLive((current) => current.taskId !== taskId ? current : { ...current, history: "loading", historyError: "" });
    fetchChatMessages(`planner-${taskId}`, ctrl.signal).then((rows) => {
      if (ctrl.signal.aborted || turn !== gen.current || taskRef.current !== taskId) return;
      if (!Array.isArray(rows)) throw new Error("The server returned invalid chat messages.");
      if (sent !== sendGen.current) return;
      setLive((current) => current.taskId !== taskId ? current : {
        ...current,
        messages: fromDb(rows),
        history: rows.length === 0 ? "empty" : "ready",
        historyError: "",
      });
    }).catch((error: unknown) => {
      if (ctrl.signal.aborted || turn !== gen.current || (error instanceof Error && error.name === "AbortError")) return;
      setLive((current) => current.taskId !== taskId ? current : {
        ...current,
        history: "error",
        historyError: errorText(error),
      });
    });
    return () => ctrl.abort();
  }, [taskId, missing, retry]);

  const restoreDraft = (extra: string[] = []) => {
    const list = [...extra, ...stagedRef.current];
    if (list.length === 0) return;
    setDraft((value) => pullBack(list, value));
    setStaged([]);
  };

  const send = (text: string) => {
    if (!text.trim() || !canSendRef.current || missing || taskRef.current !== taskId) return;
    const turn = gen.current;
    const sid = `planner-${taskId}`;
    const target: ChatTarget = { kind: "planner", task_id: taskId };
    sendGen.current += 1;
    finalSeen.current = false;
    reply.cancel();
    const id = randomId();
    const assistantId = `${id}:reply`;
    setLive((current) => ({
      ...current,
      taskId,
      history: current.history === "loading" ? "ready" : current.history === "empty" ? "ready" : current.history,
      notice: "",
      phase: "",
      replying: true,
      messages: [
        ...current.messages,
        { id, role: "user", content: text },
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ],
    }));
    const still = { current: () => false };
    reply.start({
      target,
      threadId: sid,
      messageId: id,
      text,
      ownerId: taskId,
      isCurrent: still,
      onEvent: (event) => {
        if (turn !== gen.current || taskRef.current !== taskId || !still.current()) return;
        if (event.kind === "answer.delta") {
          const chunk = String(event.payload.text ?? "");
          setLive((current) => ({
            ...current,
            messages: current.messages.map((message) => message.id === assistantId
              ? { ...message, content: message.content + chunk }
              : message),
          }));
        } else if (event.kind === "agent.phase") {
          setLive((current) => ({ ...current, phase: String(event.payload.label ?? "") }));
        } else if (event.kind === "answer.final") {
          finalSeen.current = true;
          const finalText = String(event.payload.text ?? "");
          const outcome = String(event.payload.outcome ?? "answered");
          setLive((current) => ({
            ...current,
            phase: "",
            replying: false,
            messages: current.messages.map((message) => message.id === assistantId ? {
              ...message,
              content: finalText || message.content || "Done.",
              streaming: false,
              outcome: outcome === "stopped" ? "stopped" : undefined,
            } : message),
          }));
          const queued = stagedRef.current;
          if (queued.length === 0) return;
          if (outcome === "answered" && canSendRef.current) {
            setStaged([]);
            sendRef.current(joinStaged(queued));
          } else restoreDraft();
        }
      },
      onDone: () => {
        if (turn !== gen.current || taskRef.current !== taskId || !still.current()) return;
        const disconnected = !finalSeen.current;
        setLive((current) => ({
          ...current,
          replying: false,
          phase: "",
          notice: disconnected ? STREAM_LOST : current.notice,
          messages: current.messages.map((message) => message.id === assistantId ? { ...message, streaming: false } : message),
        }));
        if (!disconnected) return;
        restoreDraft();
      },
      onError: (error) => {
        if (turn !== gen.current || taskRef.current !== taskId || !still.current()) return;
        setLive((current) => ({
          ...current,
          replying: false,
          phase: "",
          notice: sendFailure(error),
          messages: current.messages.filter((message) => message.id !== id && message.id !== assistantId),
        }));
        restoreDraft([text]);
      },
    });
  };
  sendRef.current = send;

  const view = live.taskId === taskId ? live : liveRef.current;
  return (
    <WorkConversation
      identity={identity}
      missing={missing}
      canSend={availabilityView.canSend && !missing}
      refusal={availabilityView.reason || undefined}
      detail={detail}
      messages={view.messages}
      history={view.history}
      historyError={view.historyError}
      onRetry={() => setRetry((n) => n + 1)}
      phase={view.phase}
      replying={view.replying}
      draft={shownDraft}
      onDraft={setDraft}
      staged={shownStaged}
      onStaged={setStaged}
      inputHistory={historyFrom(view.messages)}
      onSend={(text) => {
        setDraft("");
        send(text);
      }}
      onStop={() => {}}
      notice={view.notice || undefined}
      canStop={false}
    />
  );
}
