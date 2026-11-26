import { useEffect, useRef, useState } from "react";
import { ChatComposer, type ComposerCopy } from "@/components/chat-composer";
import { WorkActivityFeed, WorkMessageRow, WorkSinceAway, type WorkFeedRecord } from "@/components/work-activity-feed";
import { Button } from "@/components/ui/button";
import {
  fetchChatMessages,
  fetchChatTargets,
  type ChatTarget,
  type DbChatMessage,
  type SSEEvent,
  type TaskDetail,
  type UserGate,
} from "@/lib/api";
import { historyFrom, joinStaged, pullBack } from "@/lib/composer";
import { randomId } from "@/lib/ids";
import { useReplyStream } from "@/lib/use-reply-stream";
import { useWorkManagerConversation } from "@/lib/use-work-manager-conversation";
import { useWorkTaskConversation, type TaskConversationInput } from "@/lib/work-task-conversation";
import { errorText } from "@/lib/use-action";
import { clearWorkConversationMemory, useWorkConversationState, useWorkConversationViewport } from "@/lib/use-work-conversation-state";

/** The owner of the conversation shown in the pane. */
export type WorkIdentity =
  | { kind: "manager"; managerId?: string; sessionId?: string }
  | { kind: "planner"; taskId: string; sessionId?: string }
  | { kind: "task"; taskId: string; sessionId?: string };

export interface WorkPaneMessage {
  id: string;
  role: string;
  content: string;
  created_at?: string;
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
  task?: TaskDetail;
  onGateChanged?: (gate: UserGate) => void;
  records?: WorkFeedRecord[];
  events?: SSEEvent[];
  history: "loading" | "empty" | "ready" | "error";
  historyError?: string;
  loadingText?: string;
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
  stopRefusal?: string;
  /** Task thread only. Omitted, the pane keeps the Planner "recent messages" line and no earlier page. */
  earlier?: "hidden" | "ready" | "loading" | "error";
  earlierError?: string;
  onLoadEarlier?: () => void;
  /** Server `turn_state`. Absent unless this pane is a task thread. */
  turnState?: "idle" | "running" | "interrupted";
  banner?: string;
}

export const WORK_COMPOSER_COPY: ComposerCopy = {
  idle: "输入消息…（Shift+Enter 换行）", replying: "正在回复… 按 Enter 加入队列",
  stoppable: "正在回复… 按 Enter 加入队列，Esc 停止", full: "队列已满",
  queued: "已排队 · 回复结束后发送", edit: "编辑", remove: "移除排队消息", message: "消息",
  stop: "停止回复（Esc）", stopReason: "停止回复", queue: "加入队列", queueButton: "加入队列", send: "发送",
};

const RECENT = "仅显示最近的对话";
const EMPTY = "还没有对话";
const LOADING = "正在加载对话…";
const READ_FAILED = "对话读取失败";
const NOT_FOUND = "任务不存在";
const CHECKING = "正在检查对话可用性";
const CHECK_FAILED = "无法检查对话可用性";
const NO_PLANNER = "任务管家当前不可用，暂时无法接收消息。";
const PLANNER_ENDED = "任务已结束，任务管家不再接收消息。";
const TERMINAL = new Set(["completed", "failed", "cancelled", "stopped"]);
const CONNECT_FAILED = "连接失败，请检查服务器是否运行。";
const STREAM_LOST = "回复流已断开，服务端可能仍在运行";

export const clearWorkComposerMemory = clearWorkConversationMemory;

function fromDb(rows: DbChatMessage[]): WorkPaneMessage[] {
  return rows.map((row) => ({
    id: row.message_id,
    role: row.role,
    content: row.content,
    created_at: row.created_at,
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
      row.content = text || row.content || "已完成。";
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

/**
 * The Work centre pane. An adapter supplies identity, whether sending is allowed,
 * the shared task, stream events and the send/stop callbacks.
 */
export function WorkConversation(props: WorkConversationProps) {
  const {
    identity, missing, canSend, refusal, detail, messages, events, history, historyError, loadingText, onRetry,
    phase, replying, draft, onDraft, staged, onStaged, inputHistory, onSend, onStop, notice, canStop, stopRefusal,
  } = props;
  const ownerKey = identity.kind === "manager" ? `manager:${identity.sessionId ?? ""}` : `${identity.kind}:${identity.taskId}`;
  const position = useWorkConversationViewport(identity, history === "ready" || history === "empty" || messages.length > 0);
  if (missing) return <p role="status" data-identity={ownerKey}>{NOT_FOUND}</p>;

  const folded = foldStream(messages, events ?? []);
  const shownPhase = phase || folded.phase;
  const shown = folded.messages;
  const loaded = history === "ready" || history === "empty";

  return (
    <section
      aria-label="对话"
      data-identity={ownerKey}
      data-task-status={detail?.status ?? ""}
      data-turn-state={props.turnState}
      className="flex h-full min-h-0 flex-1 flex-col"
    >
      <div ref={position.viewport} aria-label="对话历史" className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4 [overflow-anchor:none]">
        {history === "loading" && shown.length === 0 && <p role="status">{loadingText ?? LOADING}</p>}
        {history === "error" && (
          <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
            <span>{READ_FAILED} {historyError}</span>
            <Button type="button" variant="outline" size="sm" onClick={onRetry}>重试</Button>
          </p>
        )}
        {props.banner && <p role="status" className="text-sm text-muted-foreground">{props.banner}</p>}
        {(props.earlier === "ready" || props.earlier === "loading" || props.earlier === "error") && (
          <div className="flex flex-wrap items-center gap-2">
            {props.earlier === "error" && <p role="alert" className="text-sm text-destructive">更早的对话读取失败 {props.earlierError}</p>}
            <Button type="button" variant="outline" size="sm" disabled={props.earlier === "loading"} onClick={props.onLoadEarlier}>
              {props.earlier === "loading" ? "正在加载更早对话…" : "加载更早对话"}
            </Button>
          </div>
        )}
        {loaded && props.earlier === undefined && <p className="text-center text-xs text-muted-foreground">{RECENT}</p>}
        {loaded && shown.length === 0 && <p role="status">{EMPTY}</p>}
        {props.task ? <WorkActivityFeed identity={identity} task={props.task} messages={shown}
          onGateChanged={props.onGateChanged} records={props.records} dividerBefore={position.divider} />
          : (history !== "error" || shown.length > 0) && shown.map(message => <div key={message.id} data-feed-key={`message:${message.id}`}
            data-row-time={Date.parse(message.created_at ?? "") || undefined} className="shrink-0">
            {position.divider === `message:${message.id}` && <WorkSinceAway />}
            <WorkMessageRow message={message} />
          </div>)}
      </div>
      <div className="shrink-0 space-y-2 border-t border-border p-4">
        {refusal && <p role="status" className="text-xs text-muted-foreground">{refusal}</p>}
        {shownPhase && <p role="status" data-phase={shownPhase}>{shownPhase}</p>}
        <ChatComposer
          key={ownerKey}
          draft={draft}
          onDraft={onDraft}
          staged={staged}
          onStaged={onStaged}
          history={inputHistory ?? historyFrom(shown)}
          replying={replying ?? false}
          canStop={canStop ?? false}
          stopRefusal={stopRefusal}
          onSend={text => { onSend(text); position.afterSend(); }}
          onStop={onStop ?? (() => {})}
          notice={notice}
          disabled={!canSend}
          controls="neutral"
          copy={WORK_COMPOSER_COPY}
        />
      </div>
    </section>
  );
}

/** Manager adapter mounts only while the gated Work page selects Manager. */
export function ManagerWorkConversation() {
  return <WorkConversation {...useWorkManagerConversation()} />;
}

/** Task-thread adapter. The Work page mounts it only while `tasks.conversations.enabled` is on. */
export function TaskWorkConversation(props: TaskConversationInput) {
  return <WorkConversation {...useWorkTaskConversation(props)} />;
}

/**
 * Planner adapter for one selected task. The thread is `planner-<task id>` and the
 * send target is `{kind:'planner', task_id}`. There is no Stop and no turn poll.
 */
export function PlannerWorkConversation({
  taskId,
  detail = null,
  task,
  onGateChanged,
  missing = false,
}: {
  taskId: string;
  detail?: WorkConversationDetail | null;
  task?: TaskDetail;
  onGateChanged?: (gate: UserGate) => void;
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

  const conversation = useWorkConversationState(identity);
  const { store, key: cacheKey } = conversation;
  const shownDraft = missing ? "" : conversation.value.draft;
  const shownStaged = missing ? [] : conversation.value.staged;
  const draftRef = useRef(shownDraft);
  const stagedRef = useRef(shownStaged);
  draftRef.current = shownDraft;
  stagedRef.current = shownStaged;
  const setDraft = (next: string | ((value: string) => string)) => {
    const value = typeof next === "function" ? next(draftRef.current) : next;
    draftRef.current = value;
    conversation.setDraft(value);
  };
  const setStaged = (next: string[]) => {
    stagedRef.current = next;
    conversation.setStaged(next);
  };
  const cached = missing ? undefined : store.cached(cacheKey);
  const [live, setLive] = useState({
    taskId,
    messages: cached?.messages ?? [] as WorkPaneMessage[],
    history: (missing ? "empty" : cached?.messagesLoaded ? (cached.messages.length ? "ready" : "empty") : "loading") as WorkConversationProps["history"],
    historyError: "",
    phase: "",
    notice: "",
    replying: false,
  });
  if (live.taskId !== taskId) {
    setLive({
      taskId,
      messages: cached?.messages ?? [],
      history: missing ? "empty" : cached?.messagesLoaded ? (cached.messages.length ? "ready" : "empty") : "loading",
      historyError: "",
      phase: "",
      notice: "",
      replying: false,
    });
  }
  const liveRef = useRef(live);
  liveRef.current = live.taskId === taskId ? live : {
    taskId, messages: cached?.messages ?? [], history: "loading" as const, historyError: "", phase: "", notice: "", replying: false,
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
  const backgroundActive = detail?.group === "in_progress" || detail?.group === "needs_you";
  const taskStatus = detail?.status;

  useEffect(() => () => { replyRef.current.cancel(); }, [taskId]);

  useEffect(() => {
    if (missing) return;
    const ctrl = new AbortController();
    const turn = gen.current;
    setAvailability({ canSend: false, reason: CHECKING });
    fetchChatTargets(ctrl.signal).then((targets) => {
      if (ctrl.signal.aborted || turn !== gen.current || taskRef.current !== taskId) return;
      const planner = targets.find((target) => target.kind === "planner" && target.task_id === taskId);
      const status = taskStatus ?? planner?.status ?? "";
      const unavailable = TERMINAL.has(status) ? PLANNER_ENDED : status === "handing_back"
        ? "任务正在交还，任务管家暂时不接收消息。" : NO_PLANNER;
      setAvailability({
        canSend: planner?.can_send ?? false,
        reason: planner?.can_send ? "发送消息与任务管家对话。" : unavailable,
      });
    }).catch((error: unknown) => {
      if (ctrl.signal.aborted || turn !== gen.current || (error instanceof Error && error.name === "AbortError")) return;
      const reason = errorText(error).trim();
      setAvailability({ canSend: false, reason: reason || CHECK_FAILED });
    });
    return () => ctrl.abort();
  }, [taskId, missing, backgroundActive, taskStatus]);

  useEffect(() => {
    if (missing) return;
    const ctrl = new AbortController();
    const turn = gen.current;
    const sent = sendGen.current;
    setLive((current) => current.taskId !== taskId ? current : { ...current, history: store.cached(cacheKey)?.messagesLoaded ? current.history : "loading", historyError: "" });
    fetchChatMessages(`planner-${taskId}`, ctrl.signal).then((rows) => {
      if (ctrl.signal.aborted || turn !== gen.current || taskRef.current !== taskId) return;
      if (!Array.isArray(rows)) throw new Error("服务器返回了无效的对话消息。");
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
        historyError: `${current.messages.length ? "刷新失败：" : ""}${errorText(error)}`,
      });
    });
    return () => ctrl.abort();
  }, [taskId, missing, retry]);

  useEffect(() => {
    if (live.taskId !== taskId || missing || live.history === "loading") return;
    store.cache(cacheKey, { messages: live.messages });
    const history = historyFrom(live.messages);
    if (history.length) conversation.rememberHistory(history);
  }, [live, taskId, missing, store, cacheKey]);

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
    const created_at = new Date().toISOString();
    setLive((current) => ({
      ...current,
      taskId,
      history: current.history === "loading" ? "ready" : current.history === "empty" ? "ready" : current.history,
      notice: "",
      phase: "",
      replying: true,
      messages: [
        ...current.messages,
        { id, role: "user", content: text, created_at },
        { id: assistantId, role: "assistant", content: "", streaming: true, created_at },
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
              content: finalText || message.content || "已完成。",
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
      task={task}
      onGateChanged={onGateChanged}
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
      inputHistory={conversation.value.history}
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
