import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  Send,
  Loader2,
  Bot,
  User,
  Plus,
  ChevronDown,
  ChevronRight,
  RotateCcw,
  MessagesSquare,
  MessageCircleQuestionMark,
} from "lucide-react";
import { Markdown } from "@/components/markdown";
import { ChannelBadge, ConversationList } from "@/components/chat-conversations";
import { channelLabel, undeliveredText } from "@/lib/chat-channels";
import {
  sendChat,
  saveChatMessage,
  fetchChatMessages,
  fetchChatSessions,
  fetchChatTargets,
  fetchTaskActivity,
  fetchTaskDetail,
  streamSSE,
  fetchRoleConfigs,
  fetchLlmRecords,
  watchChat,
  type SSEEvent,
  type RoleConfig,
  type ChatTarget,
  type ChatTargetInfo,
  type ActivityEvent,
  type ChatSession,
  type DbChatMessage,
  type LlmRecord,
  type UserGate,
} from "@/lib/api";
import { isGateWaiting } from "@/lib/gates";
import { RunCard, activityBelongsTo, historyHref, runRoleLabel } from "@/pages/agent-history";
import { cn } from "@/lib/utils";

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: number;
  streaming?: boolean;
}

const STORAGE_SESSION_KEY = "looper-chat-session-id";
const messagesKey = (sessionId: string) => `looper-chat-messages-${sessionId}`;
// The keys from before the rename (LoopX), read when the new ones are missing so an open chat carries on.
const LEGACY_SESSION_KEY = "loopx-chat-session-id";
const legacyMessagesKey = (sessionId: string) => `loopx-chat-messages-${sessionId}`;

const MANAGER_TARGET: ChatTargetInfo = { kind: "manager", id: "manager", can_send: true };
// Answered by Looper, not Claude: the Manager drops its session, so the next message, from any channel, starts clean.
const NEW_SESSION_COMMAND = "/new";
const TARGETS_POLL_MS = 10_000;
// The Planner's latest turns shown beside its chat; its history page has more.
const PLANNER_RUNS_SHOWN = 10;

// One task: its fixed Planner, then the Worker (and Reviewer) of each step.
interface TaskGroup {
  planner: ChatTargetInfo;
  agents: ChatTargetInfo[];
}

function groupByTask(targets: ChatTargetInfo[]): TaskGroup[] {
  const groups: TaskGroup[] = [];
  for (const t of targets) {
    if (t.kind === "planner") groups.push({ planner: t, agents: [] });
    else if (t.kind !== "manager") groups.find((g) => g.planner.task_id === t.task_id)?.agents.push(t);
  }
  return groups;
}

function stepAgentTitle(t: ChatTargetInfo): string {
  const role = t.task_role ? runRoleLabel(t.task_role) : t.kind === "reviewer" ? "Reviewer" : "Worker";
  return t.step_index == null ? role : `Step ${t.step_index + 1} · ${role}`;
}

function statusTone(status?: string): string {
  if (status === "running") return "text-primary";
  if (status === "completed" || status === "verified") return "text-success";
  if (status === "failed") return "text-destructive";
  return "text-muted-foreground";
}

function StatusText({ status }: { status?: string }) {
  if (!status) return null;
  return (
    <span className={cn("flex shrink-0 items-center gap-1 text-[10px]", statusTone(status))}>
      {status === "running" ? <Loader2 className="h-2.5 w-2.5 animate-spin" /> : <span className="h-1.5 w-1.5 rounded-full bg-current" />}
      {status}
    </span>
  );
}

// An agent's Claude runs, newest first and read-only: the newest starts open
// and a running one follows itself live. `pollMs` re-reads the list while the
// agent can take new turns; a changed `reloadKey` re-reads it at once. With a
// `title`, the list folds under one header line, closed at first. Mount with a
// `key` per agent so a switch starts from a clean list.
function AgentRunsView({
  agentId,
  limit,
  title,
  pollMs,
  reloadKey,
}: {
  agentId: string;
  limit: number;
  title?: string;
  pollMs?: number;
  reloadKey?: unknown;
}) {
  const [runs, setRuns] = useState<LlmRecord[] | null>(null);
  const [open, setOpen] = useState(!title);

  useEffect(() => {
    let active = true;
    const load = () =>
      fetchLlmRecords({ agent_id: agentId, limit })
        .then((next) => {
          if (active) setRuns(next);
        })
        .catch(() => {
          if (active) setRuns((prev) => prev ?? []);
        });
    load();
    const timer = pollMs ? setInterval(load, pollMs) : undefined;
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [agentId, limit, pollMs, reloadKey]);

  const list =
    runs === null ? (
      <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
    ) : runs.length === 0 ? (
      <p className="text-xs text-muted-foreground">No Claude runs recorded for this agent.</p>
    ) : (
      runs.map((record, i) => <RunCard key={record.id} initial={record} defaultOpen={i === 0} />)
    );
  if (!title) return <div className="space-y-3">{list}</div>;

  const latest = runs?.[0];
  return (
    <div className="overflow-hidden rounded-lg border border-border bg-card/50">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 p-3 text-left text-xs hover:bg-accent/40 transition-colors"
      >
        {open ? (
          <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
        )}
        <span className="font-medium">
          {title}
          {runs && ` (${runs.length < limit ? runs.length : `latest ${limit}`})`}
        </span>
        {latest && latest.status !== "completed" && <StatusText status={latest.status} />}
      </button>
      {open && <div className="space-y-3 border-t border-border p-3">{list}</div>}
    </div>
  );
}

// A Worker or Reviewer: the step it served and its runs.
function StepAgentView({ agent }: { agent: ChatTargetInfo }) {
  return (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3 text-xs">
        <p className="min-w-0 line-clamp-3 whitespace-pre-wrap text-muted-foreground" title={agent.instruction}>
          {agent.instruction || "The step this run served was not recorded."}
        </p>
        <a href={historyHref(agent.id, agent.task_id)} className="shrink-0 text-primary hover:underline">
          Open full history →
        </a>
      </div>
      <AgentRunsView key={agent.id} agentId={agent.id} limit={20} />
    </div>
  );
}

function sendTargetOf(target: ChatTargetInfo): ChatTarget {
  return target.kind === "planner" && target.task_id
    ? { kind: "planner", task_id: target.task_id }
    : { kind: "manager" };
}

// The Manager keeps the switchable session; each task's Planner has one fixed
// thread; a Worker or Reviewer has no chat, only its runs.
function sessionFor(target: ChatTargetInfo): string {
  if (target.kind === "planner") return `planner-${target.task_id}`;
  if (target.kind === "manager") return getOrCreateSessionId();
  return "";
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function getOrCreateSessionId(): string {
  const existing = localStorage.getItem(STORAGE_SESSION_KEY) ?? localStorage.getItem(LEGACY_SESSION_KEY);
  if (existing) {
    localStorage.setItem(STORAGE_SESSION_KEY, existing);
    return existing;
  }
  const id = `session-${crypto.randomUUID().slice(0, 8)}`;
  localStorage.setItem(STORAGE_SESSION_KEY, id);
  return id;
}

function loadMessages(sessionId: string): ChatMessage[] {
  try {
    const raw = localStorage.getItem(messagesKey(sessionId)) ?? localStorage.getItem(legacyMessagesKey(sessionId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((m: ChatMessage) => ({ ...m, streaming: false }));
  } catch {
    return [];
  }
}

function saveMessages(sessionId: string, messages: ChatMessage[]) {
  const toSave = messages.map(({ streaming, ...rest }) => rest);
  localStorage.setItem(messagesKey(sessionId), JSON.stringify(toSave));
}

export function ChatPage() {
  const [sessionId, setSessionId] = useState(getOrCreateSessionId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [phase, setPhase] = useState("");
  const [roleConfigs, setRoleConfigs] = useState<RoleConfig[]>([]);
  const [targets, setTargets] = useState<ChatTargetInfo[]>([MANAGER_TARGET]);
  const [target, setTarget] = useState<ChatTargetInfo>(MANAGER_TARGET);
  const [agentDropdown, setAgentDropdown] = useState(false);
  // Tasks the user opened or closed in the agent list; others follow the default.
  const [taskOpen, setTaskOpen] = useState<Record<string, boolean>>({});
  const [plannerActivity, setPlannerActivity] = useState<ActivityEvent[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const dbLoadedRef = useRef(false);
  // The Manager's conversations on every channel, and the Lark or Telegram one open read-only (null: the web thread).
  const [conversations, setConversations] = useState<ChatSession[] | null>(null);
  const [conversationsError, setConversationsError] = useState("");
  const [viewing, setViewing] = useState<ChatSession | null>(null);
  // Its messages as last read, by session id, so another conversation's never show while one loads.
  const [viewingMessages, setViewingMessages] = useState<{ id: string; rows: DbChatMessage[] } | null>(null);
  // Below the md breakpoint the list takes the messages' place while it is open.
  const [listOpen, setListOpen] = useState(false);

  const refreshConversations = useCallback(() => {
    fetchChatSessions()
      .then((rows) => {
        setConversations(rows);
        setConversationsError("");
      })
      .catch((err) => setConversationsError(err instanceof Error ? err.message : String(err)));
  }, []);

  const refreshTargets = useCallback(() => {
    fetchChatTargets().then(setTargets).catch(() => {});
  }, []);

  useEffect(() => {
    fetchRoleConfigs().then(setRoleConfigs).catch(() => {});
    refreshTargets();
    // Keeps statuses current, and stops input once a viewed Planner ends.
    const timer = setInterval(refreshTargets, TARGETS_POLL_MS);
    return () => clearInterval(timer);
  }, [refreshTargets]);

  const groups = useMemo(() => groupByTask(targets), [targets]);
  // The selected agent as last listed: its status and whether it takes messages.
  const current = targets.find((t) => t.id === target.id) ?? target;
  const taskGroup = groups.find((g) => g.planner.task_id === current.task_id);
  const isTaskOpen = (g: TaskGroup) =>
    taskOpen[g.planner.task_id ?? ""] ?? (g.planner.can_send || g.planner.task_id === current.task_id);

  const managerConfig = roleConfigs.find((c) => c.role === "manager");
  const targetTitle = (t: ChatTargetInfo) => {
    if (t.kind === "planner") return `Planner · ${truncate(t.name || t.requirement || t.task_id || "", 40)}`;
    if (t.kind === "manager") return managerConfig?.display_name || "Manager";
    return `${stepAgentTitle(t)} · ${truncate(taskGroup?.planner.name || taskGroup?.planner.requirement || t.task_id || "", 32)}`;
  };
  const targetSubtitle = (t: ChatTargetInfo) =>
    t.kind === "manager"
      ? "global"
      : `${t.kind === "planner" ? t.task_id : t.id} · ${t.status || "unknown"}`;

  // A Planner's own record is its activity; show it above the chat with it.
  useEffect(() => {
    setPlannerActivity([]);
    if (target.kind !== "planner" || !target.task_id) return;
    const taskId = target.task_id;
    const load = () =>
      fetchTaskActivity(taskId, 0, 1000)
        .then(({ activity }) => setPlannerActivity(activity.filter((ev) => activityBelongsTo(ev, `planner-${taskId}`))))
        .catch(() => {});
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [target]);

  // The gates of an open Planner's task that wait for the user, re-read on the targets cadence: answers go on the task
  // page, as what is written in this thread stays a supplement.
  const [waitingGates, setWaitingGates] = useState<{ taskId: string; gates: UserGate[] } | null>(null);
  const plannerTaskId = target.kind === "planner" ? target.task_id : undefined;
  useEffect(() => {
    if (!plannerTaskId) return;
    let alive = true;
    const load = () =>
      fetchTaskDetail(plannerTaskId)
        .then((task) => {
          // A completed task's gates take no more answers (the task page says so).
          const gates = task.group === "completed" ? [] : (task.gates ?? []).filter(isGateWaiting);
          if (alive) setWaitingGates({ taskId: plannerTaskId, gates });
        })
        .catch(() => {});
    load();
    const timer = setInterval(load, TARGETS_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [plannerTaskId]);
  const gatesWaiting = plannerTaskId && waitingGates?.taskId === plannerTaskId ? waitingGates.gates : [];

  // The conversation list, while the Manager is shown: re-read on the targets cadence and after each answer.
  useEffect(() => {
    if (target.kind !== "manager" || sending) return;
    refreshConversations();
    const timer = setInterval(refreshConversations, TARGETS_POLL_MS);
    return () => clearInterval(timer);
  }, [target.kind, sending, refreshConversations]);

  // A Lark or Telegram conversation is read-only here: its newest messages, re-read on the same cadence.
  const viewingId = viewing?.session_id;
  useEffect(() => {
    if (!viewingId) return;
    let alive = true;
    const load = () =>
      fetchChatMessages(viewingId)
        .then((rows) => alive && setViewingMessages({ id: viewingId, rows }))
        .catch(() => alive && setViewingMessages((prev) => (prev?.id === viewingId ? prev : { id: viewingId, rows: [] })));
    load();
    const timer = setInterval(load, TARGETS_POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [viewingId]);

  const openConversation = (session: ChatSession) => {
    setListOpen(false);
    if (session.channel !== "web") {
      setViewing(session);
      return;
    }
    // A dashboard chat thread opens as the web chat, as if the page had been opened on it.
    setViewing(null);
    if (session.session_id === sessionId) return;
    cleanupRef.current?.();
    cleanupRef.current = null;
    localStorage.setItem(STORAGE_SESSION_KEY, session.session_id);
    setSessionId(session.session_id);
    setMessages([]);
    setSending(false);
    setPhase("");
  };

  const selectTarget = (next: ChatTargetInfo) => {
    setAgentDropdown(false);
    setViewing(null);
    setListOpen(false);
    if (next.id === target.id) return;
    cleanupRef.current?.();
    cleanupRef.current = null;
    setSending(false);
    setPhase("");
    setMessages([]);
    setTarget(next);
    setSessionId(sessionFor(next));
  };

  const scrollToBottom = useCallback(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, []);

  useEffect(scrollToBottom, [messages, scrollToBottom]);
  // A read-only conversation opens at its newest message, and follows when one arrives (not on every re-read).
  const viewingShown = viewingMessages ? `${viewingMessages.id}:${viewingMessages.rows.length}` : "";
  useEffect(scrollToBottom, [viewingShown, scrollToBottom]);

  useEffect(() => {
    if (!sending) inputRef.current?.focus();
  }, [sending]);

  // The message box grows with its lines, up to its max height, and shrinks back once sent.
  useLayoutEffect(() => {
    const box = inputRef.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${box.scrollHeight}px`;
  }, [input]);

  // Load messages from DB on mount (or session change)
  useEffect(() => {
    dbLoadedRef.current = false;
    if (!sessionId) {
      setMessages([]);
      dbLoadedRef.current = true;
      return;
    }
    fetchChatMessages(sessionId).then((dbMsgs) => {
      if (dbMsgs.length > 0) {
        const loaded: ChatMessage[] = dbMsgs.map((m) => ({
          id: m.message_id,
          role: m.role as "user" | "assistant" | "system",
          content: m.content,
          timestamp: new Date(m.created_at).getTime(),
          streaming: false,
        }));
        setMessages(loaded);
      } else {
        const local = loadMessages(sessionId);
        if (local.length > 0) setMessages(local);
      }
      dbLoadedRef.current = true;
    }).catch(() => {
      const local = loadMessages(sessionId);
      if (local.length > 0) setMessages(local);
      dbLoadedRef.current = true;
    });
  }, [sessionId]);

  // What the Manager's Claude says on its own (after background work it started
  // ends) answers no message, so no message's stream carries it: it arrives on
  // the open chat's own stream and shows as a message of its own.
  useEffect(() => {
    if (target.kind !== "manager" || !sessionId) return;
    return watchChat(sessionId, (m) =>
      setMessages((prev) =>
        prev.some((p) => p.id === m.message_id)
          ? prev
          : [
              ...prev,
              {
                id: m.message_id,
                role: "assistant",
                content: m.content,
                timestamp: new Date(m.created_at).getTime(),
                streaming: false,
              },
            ],
      ),
    );
  }, [sessionId, target.kind]);

  // Keep localStorage as cache
  useEffect(() => {
    if (sessionId && messages.length > 0) {
      saveMessages(sessionId, messages);
    }
  }, [messages, sessionId]);

  const handleNewChat = () => {
    setViewing(null);
    cleanupRef.current?.();
    cleanupRef.current = null;
    const newId = `session-${crypto.randomUUID().slice(0, 8)}`;
    localStorage.setItem(STORAGE_SESSION_KEY, newId);
    setSessionId(newId);
    setMessages([]);
    setSending(false);
    setPhase("");
    setInput("");
  };

  const handleSend = async (command?: string) => {
    const text = command ?? input.trim();
    if (!text || sending || !current.can_send) return;

    if (command === undefined) setInput("");
    setSending(true);
    setPhase("");

    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      timestamp: Date.now(),
    };
    setMessages((prev) => [...prev, userMsg]);

    // The server saves the answer to the chat under this id (`reply_message_id`), even when the page is left before
    // it comes: the stream's bubble and the saved row are the same message.
    const assistantId = `${userMsg.id}:reply`;
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      timestamp: Date.now(),
      streaming: true,
    };
    setMessages((prev) => [...prev, assistantMsg]);

    try {
      const { events_url } = await sendChat(text, sessionId, userMsg.id, sendTargetOf(target));

      cleanupRef.current = streamSSE(
        events_url,
        (event: SSEEvent) => {
          switch (event.kind) {
            case "answer.delta":
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: m.content + ((event.payload as any).text || "") }
                    : m
                )
              );
              break;
            case "answer.final": {
              const finalText = (event.payload as any).text || "";
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? { ...m, content: finalText || m.content || "Done.", streaming: false }
                    : m
                )
              );
              setSending(false);
              setPhase("");
              break;
            }
            case "agent.phase":
              setPhase((event.payload as any).label || "");
              break;
            case "turn.completed":
            case "turn.failed":
              setMessages((prev) => {
                const msg = prev.find((m) => m.id === assistantId);
                if (msg && msg.content) {
                  saveChatMessage({
                    session_id: sessionIdRef.current,
                    message_id: assistantId,
                    role: "assistant",
                    content: msg.content,
                  });
                }
                return prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m));
              });
              setSending(false);
              setPhase("");
              break;
          }
        },
        () => {
          setSending(false);
          setPhase("");
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m))
          );
        }
      );
    } catch (err) {
      // fetch() rejects with a TypeError when the server is unreachable; other
      // errors carry the server's reason, e.g. the task's Planner has stopped.
      const reason =
        err instanceof TypeError || !(err instanceof Error)
          ? "Failed to connect. Is the server running?"
          : err.message;
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantId ? { ...m, content: reason, streaming: false } : m
        )
      );
      setSending(false);
      if (target.kind === "planner") refreshTargets();
    }
  };

  const managerView = target.kind === "manager";
  const conversationList = (
    <ConversationList
      sessions={conversations}
      error={conversationsError}
      selected={viewing?.session_id ?? sessionId}
      onSelect={openConversation}
      onRefresh={refreshConversations}
    />
  );

  return (
    <div className="flex h-full">
      {managerView && <aside className="hidden w-72 shrink-0 border-r border-border md:block">{conversationList}</aside>}
      <div className="flex h-full min-w-0 flex-1 flex-col">
      <div className="border-b border-border px-5 py-3">
        <div className="flex items-center gap-2">
          {managerView && (
            <button
              type="button"
              onClick={() => setListOpen(!listOpen)}
              aria-expanded={listOpen}
              className="flex h-8 w-8 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-accent hover:text-foreground transition-colors md:hidden"
              title="Conversations"
              aria-label="Conversations"
            >
              <MessagesSquare className="h-4 w-4" />
            </button>
          )}
          {/* Agent selector */}
          <div className="relative">
            <button
              type="button"
              onClick={() => {
                if (!agentDropdown) refreshTargets();
                setAgentDropdown(!agentDropdown);
              }}
              className="flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-accent transition-colors"
            >
              <div className="flex h-7 w-7 items-center justify-center rounded-full bg-primary/10">
                <Bot className="h-3.5 w-3.5 text-primary" />
              </div>
              <div className="text-left">
                <p className="text-sm font-semibold leading-tight">{targetTitle(current)}</p>
                <p className="text-[10px] text-muted-foreground leading-tight">{targetSubtitle(current)}</p>
              </div>
              <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" />
            </button>
            {agentDropdown && (
              <div className="absolute left-0 top-full z-50 mt-1 max-h-[70vh] w-96 overflow-y-auto rounded-lg border border-border bg-popover py-1 shadow-lg">
                <AgentRow
                  title={targetTitle(MANAGER_TARGET)}
                  subtitle={targetSubtitle(MANAGER_TARGET)}
                  selected={target.id === MANAGER_TARGET.id}
                  onSelect={() => selectTarget(MANAGER_TARGET)}
                />
                <p className="mt-1 border-t border-border px-3 pb-1 pt-2 font-mono text-[10px] uppercase tracking-wider text-muted-foreground">
                  Tasks
                </p>
                {groups.length === 0 && (
                  <p className="px-3 py-2 text-xs text-muted-foreground">
                    No tasks yet. Each task lists its Planner and the Worker of every step here.
                  </p>
                )}
                {groups.map((g) => {
                  const taskId = g.planner.task_id ?? "";
                  const open = isTaskOpen(g);
                  return (
                    <div key={taskId}>
                      <button
                        type="button"
                        onClick={() => setTaskOpen((prev) => ({ ...prev, [taskId]: !open }))}
                        aria-expanded={open}
                        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs hover:bg-accent transition-colors"
                        title={g.planner.requirement}
                      >
                        {open ? (
                          <ChevronDown className="h-3 w-3 shrink-0 text-muted-foreground" />
                        ) : (
                          <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground" />
                        )}
                        <span className="min-w-0 flex-1 truncate font-medium">{g.planner.name || g.planner.requirement || taskId}</span>
                        <StatusText status={g.planner.status} />
                      </button>
                      {open && (
                        <div className="pb-1">
                          <AgentRow
                            nested
                            title="Planner"
                            subtitle={g.planner.can_send ? "takes messages" : "task ended · read-only"}
                            status={g.planner.can_send ? "running" : undefined}
                            selected={target.id === g.planner.id}
                            onSelect={() => selectTarget(g.planner)}
                          />
                          {g.agents.map((a) => (
                            <AgentRow
                              key={a.id}
                              nested
                              title={stepAgentTitle(a)}
                              subtitle={a.instruction || a.id}
                              status={a.status}
                              selected={target.id === a.id}
                              onSelect={() => selectTarget(a)}
                            />
                          ))}
                          {g.agents.length === 0 && (
                            <p className="py-1 pl-9 pr-3 text-[10px] text-muted-foreground">No step has started yet.</p>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="ml-auto flex items-center gap-3">
            {viewing && (
              <span className="flex items-center gap-2 whitespace-nowrap text-xs text-muted-foreground">
                <ChannelBadge channel={viewing.channel} />
                Read-only
              </span>
            )}
            {!viewing && sessionId && (
              <span className="hidden font-mono text-[11px] text-muted-foreground sm:inline">{sessionId}</span>
            )}
            {target.kind === "manager" && !viewing && (
              <button
                type="button"
                onClick={() => handleSend(NEW_SESSION_COMMAND)}
                disabled={sending}
                className="flex h-7 items-center gap-1.5 rounded-md border border-border px-2 text-xs text-muted-foreground hover:text-foreground hover:bg-accent transition-colors disabled:opacity-50"
                title="Send /new: the Manager starts a new session, and its next message, from any channel, starts with a clean context"
              >
                <RotateCcw className="h-3.5 w-3.5" />
                New session
              </button>
            )}
            {target.kind === "manager" && !viewing && (
              <button
                type="button"
                onClick={handleNewChat}
                className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                title="New conversation: a new chat thread; the Manager's session goes on"
              >
                <Plus className="h-3.5 w-3.5" />
              </button>
            )}
            <div className="flex items-center gap-1.5">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-75" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
              </span>
              <span className="text-xs text-muted-foreground">Online</span>
            </div>
          </div>
        </div>
        {current.kind === "planner" && current.can_send && (
          <p className="text-xs text-muted-foreground mt-1.5">
            Messages go to this task's Planner (Claude). It replies, and can pass your message to the Worker running the current step or add steps to the plan.
          </p>
        )}
        {phase && (
          <p className="text-xs text-muted-foreground mt-1.5 flex items-center gap-1">
            <Loader2 className="h-3 w-3 animate-spin" />
            {phase}
          </p>
        )}
      </div>

      {plannerTaskId && gatesWaiting.length > 0 && (
        <div className="border-b border-border px-5 py-3">
          <div
            role="status"
            className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-xs text-foreground"
          >
            <MessageCircleQuestionMark className="h-4 w-4 shrink-0 text-warning" aria-hidden="true" />
            <p className="min-w-0 flex-1 basis-60 [overflow-wrap:anywhere]">
              {gatesWaiting.map((gate) => (
                <span key={gate.step_id} className="block">
                  This task waits for your answer at gate <span className="font-mono">{gate.step}</span>: {gate.topic}
                  {/[.?!。？！]$/.test(gate.topic.trim()) ? "" : "."}
                </span>
              ))}
              <span className="block">
                Answer {gatesWaiting.length > 1 ? "them" : "it"} on the task page
                {current.can_send ? "; what you write here goes to the Planner as a supplement." : "."}
              </span>
            </p>
            <a
              href={`#tasks/${encodeURIComponent(plannerTaskId)}`}
              className="inline-flex min-h-11 shrink-0 items-center rounded-md border border-border bg-background px-3 text-xs font-medium text-foreground transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Open the task page
            </a>
          </div>
        </div>
      )}

      {managerView && listOpen && <div className="min-h-0 flex-1 md:hidden">{conversationList}</div>}

      <div
        ref={scrollRef}
        className={cn("flex-1 overflow-y-auto px-5 py-4 space-y-4", managerView && listOpen && "hidden md:block")}
        onClick={() => agentDropdown && setAgentDropdown(false)}
      >
        {target.kind === "planner" && target.task_id && (
          <div className="rounded-lg border border-border bg-card/50 p-3">
            <div className="mb-2 flex items-center justify-between text-xs">
              <span className="font-medium">Planner activity ({plannerActivity.length})</span>
              <a href={historyHref(`planner-${target.task_id}`, target.task_id)} className="text-primary hover:underline">
                Open full history →
              </a>
            </div>
            {plannerActivity.length === 0 ? (
              <p className="text-xs text-muted-foreground">No activity yet.</p>
            ) : (
              <div className="max-h-48 space-y-1 overflow-y-auto">
                {plannerActivity.slice(-15).map((ev) => (
                  <div key={ev.seq} className="flex items-start gap-2 text-xs">
                    <span className="shrink-0 font-mono tabular-nums text-muted-foreground">
                      {new Date(ev.created_at).toLocaleTimeString([], { hour12: false })}
                    </span>
                    <span className={cn("min-w-0 break-words", ev.kind === "error" && "text-destructive")}>
                      {ev.summary || ev.kind}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* The Planner's turns: re-read as a reply starts and ends, and on the
            targets cadence while it runs (it also takes turns on step results). */}
        {target.kind === "planner" && target.task_id && (
          <AgentRunsView
            key={target.id}
            agentId={`planner-${target.task_id}`}
            limit={PLANNER_RUNS_SHOWN}
            title="Claude runs"
            pollMs={current.can_send ? TARGETS_POLL_MS : undefined}
            reloadKey={sending}
          />
        )}

        {(target.kind === "worker" || target.kind === "reviewer") && <StepAgentView agent={current} />}

        {viewing && (
          <ChannelMessages
            session={conversations?.find((c) => c.session_id === viewing.session_id) ?? viewing}
            messages={viewingMessages?.id === viewing.session_id ? viewingMessages.rows : null}
          />
        )}

        {messages.length === 0 && target.kind === "manager" && !viewing && (
          <div className="flex h-full items-center justify-center">
            <div className="text-center">
              <Bot className="mx-auto h-12 w-12 text-muted-foreground/50" />
              <p className="mt-3 text-sm text-muted-foreground">Send a message to start</p>
            </div>
          </div>
        )}

        {!viewing && messages.map((msg) => (
          <div
            key={msg.id}
            className={cn("flex gap-3", msg.role === "user" ? "justify-end" : "justify-start")}
          >
            {msg.role === "assistant" && (
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10">
                <Bot className="h-4 w-4 text-primary" />
              </div>
            )}
            <div
              className={cn(
                // A long line (a JSON reply, a path, a URL) wraps inside the bubble instead of widening the page.
                "min-w-0 max-w-[70%] rounded-lg px-4 py-2.5 text-sm [overflow-wrap:anywhere]",
                msg.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-foreground"
              )}
            >
              {msg.role === "assistant" ? (
                <Markdown>{msg.content || (msg.streaming ? "..." : "")}</Markdown>
              ) : (
                <p className="whitespace-pre-wrap">{msg.content}</p>
              )}
              {msg.streaming && (
                <Loader2 className="mt-1.5 h-3 w-3 animate-spin text-muted-foreground" />
              )}
            </div>
            {msg.role === "user" && (
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent">
                <User className="h-4 w-4 text-muted-foreground" />
              </div>
            )}
          </div>
        ))}
      </div>

      <div className={cn("border-t border-border p-4", managerView && listOpen && "hidden md:block")}>
        {viewing ? (
          <p className="text-xs text-muted-foreground">
            This conversation is in {channelLabel(viewing.channel)}, where the Manager answers it; here it is read-only.
          </p>
        ) : !current.can_send ? (
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            <span className="flex-1">
              {current.kind === "planner"
                ? "This task has ended, so its Planner no longer takes messages."
                : taskGroup?.planner.can_send
                  ? "Workers take messages through their task's Planner, which decides whether to pass them to the running step."
                  : `This ${current.kind === "reviewer" ? "Reviewer" : "Worker"}'s task has ended; its runs are read-only.`}
            </span>
            {current.kind !== "planner" && taskGroup?.planner.can_send && (
              <button
                type="button"
                onClick={() => selectTarget(taskGroup.planner)}
                className="rounded-md border border-border px-3 py-1.5 text-foreground hover:bg-accent transition-colors"
              >
                Message the Planner
              </button>
            )}
          </div>
        ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            handleSend();
          }}
          className="flex items-end gap-2"
        >
          <textarea
            ref={inputRef}
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              // Enter sends and Shift+Enter starts a new line; an input method's Enter only picks its candidate
              // (Safari reports that keydown with keyCode 229 rather than isComposing).
              if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing || e.keyCode === 229) return;
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }}
            placeholder="Type a message... (Shift+Enter for a new line)"
            aria-label="Message"
            className="max-h-40 min-h-10 flex-1 resize-none overflow-y-auto rounded-lg border border-input bg-background px-4 py-2.5 text-sm leading-5 text-foreground placeholder:text-muted-foreground outline-none focus:ring-2 focus:ring-ring"
            autoFocus
          />
          <button
            type="submit"
            disabled={sending || !input.trim()}
            className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
          >
            {sending ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Send className="h-4 w-4" />
            )}
          </button>
        </form>
        )}
      </div>
      </div>
    </div>
  );
}

/** A Lark or Telegram conversation's newest messages, read-only, each with its time. */
function ChannelMessages({ session, messages }: { session: ChatSession; messages: DbChatMessage[] | null }) {
  if (messages === null) {
    return (
      <div className="flex justify-center py-10">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (messages.length === 0) {
    return <p className="py-10 text-center text-sm text-muted-foreground">No messages in this conversation.</p>;
  }
  return (
    <>
      {session.message_count > messages.length && (
        <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-center text-xs text-muted-foreground">
          Showing the newest {messages.length} of {session.message_count} messages.
        </p>
      )}
      {messages.map((msg) => {
        const isUser = msg.role === "user";
        const undelivered = undeliveredText(session.channel, msg.delivery_failure, msg.delivery_error);
        return (
          <div key={msg.message_id} className={cn("flex gap-3", isUser ? "justify-end" : "justify-start")}>
            {!isUser && (
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10">
                <Bot className="h-4 w-4 text-primary" />
              </div>
            )}
            <div className={cn("flex min-w-0 max-w-[70%] flex-col", isUser ? "items-end" : "items-start")}>
              <div
                className={cn(
                  "min-w-0 rounded-lg px-4 py-2.5 text-sm [overflow-wrap:anywhere]",
                  isUser ? "bg-primary text-primary-foreground" : "bg-muted text-foreground",
                )}
              >
                {isUser ? <p className="whitespace-pre-wrap">{msg.content}</p> : <Markdown>{msg.content}</Markdown>}
              </div>
              <time dateTime={msg.created_at} className="mt-1 text-[10px] tabular-nums text-muted-foreground">
                {new Date(msg.created_at).toLocaleString([], { hour12: false })}
              </time>
              {undelivered && (
                <p role="status" className="mt-1 text-xs text-destructive [overflow-wrap:anywhere]">
                  {undelivered}
                </p>
              )}
            </div>
            {isUser && (
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent">
                <User className="h-4 w-4 text-muted-foreground" />
              </div>
            )}
          </div>
        );
      })}
    </>
  );
}

function AgentRow({
  title,
  subtitle,
  status,
  selected,
  nested,
  onSelect,
}: {
  title: string;
  subtitle: string;
  status?: string;
  selected: boolean;
  nested?: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "flex w-full items-center gap-2 py-1.5 pr-3 text-left text-sm hover:bg-accent transition-colors",
        nested ? "pl-9" : "pl-3",
        selected && "bg-accent/50",
      )}
    >
      {!nested && <Bot className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
      <div className="min-w-0 flex-1">
        <p className={cn("truncate", nested ? "text-xs" : "font-medium")}>{title}</p>
        <p className="truncate text-[10px] text-muted-foreground" title={subtitle}>{subtitle}</p>
      </div>
      <StatusText status={status} />
      {selected && <span className="text-xs text-primary">✓</span>}
    </button>
  );
}
