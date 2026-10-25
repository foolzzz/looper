import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Square,
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
import { ChatComposer } from "@/components/chat-composer";
import { NewSessionPanel } from "@/components/new-session-panel";
import { ManagerSessionProfile } from "@/components/manager-session-profile";
import { Markdown } from "@/components/markdown";
import { ChannelBadge, ConversationList } from "@/components/chat-conversations";
import { channelLabel, undeliveredText } from "@/lib/chat-channels";
import {
  sendChat,
  fetchChatMessages,
  fetchChatSettings,
  fetchChatTurn,
  stopChatReply,
  fetchChatSession,
  fetchChatSessions,
  ForeignConversationError,
  fetchChatTargets,
  fetchManagerSession,
  fetchTaskActivity,
  fetchTaskDetail,
  streamChatReply,
  fetchRoleConfigs,
  fetchLlmRecords,
  watchChat,
  type SSEEvent,
  type RoleConfig,
  type ChatTarget,
  type ChatTargetInfo,
  type ActivityEvent,
  type ChatSession,
  type ChatTurn,
  type DbChatMessage,
  type LlmRecord,
  type UserGate,
  type ManagerSessionView,
  sessionProfileLabel,
} from "@/lib/api";
import { historyFrom, joinStaged, pullBack, turnAnswers } from "@/lib/composer";
import { isGateWaiting } from "@/lib/gates";
import { randomId } from "@/lib/ids";
import { RunCard, activityBelongsTo, historyHref, runRoleLabel } from "@/pages/agent-history";
import { cn } from "@/lib/utils";

interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: number;
  streaming?: boolean;
  /** How the turn ended when it was not an ordinary answer ("stopped" shows a label). */
  outcome?: string;
}

const STORAGE_SESSION_KEY = "looper-chat-session-id";
const messagesKey = (sessionId: string) => `looper-chat-messages-${sessionId}`;
// The keys from before the rename (LoopX), read when the new ones are missing so an open chat carries on.
const LEGACY_SESSION_KEY = "loopx-chat-session-id";
const legacyMessagesKey = (sessionId: string) => `loopx-chat-messages-${sessionId}`;

const stagedKey = (sessionId: string) => `looper-chat-staged-${sessionId}`;
// The server's running turn is asked again this often while the page waits for it to end.
const TURN_POLL_MS = 3000;
const STOP_DISABLED_NOTICE = "Stopping a reply is off (Settings → Chat).";
const OTHER_MESSAGE_NOTICE = "The Manager is answering another message first; this one has not started.";
const CANNOT_INTERRUPT_NOTICE = "This runtime cannot interrupt a reply.";

function readStaged(sessionId: string): string[] {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(stagedKey(sessionId)) ?? "[]");
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function writeStaged(sessionId: string, staged: string[]) {
  try {
    if (staged.length === 0) sessionStorage.removeItem(stagedKey(sessionId));
    else sessionStorage.setItem(stagedKey(sessionId), JSON.stringify(staged));
  } catch {
    // Storage full or blocked: the queue still works for this page's life.
  }
}

function fromDb(rows: DbChatMessage[]): ChatMessage[] {
  return rows.map((m) => ({
    id: m.message_id,
    role: m.role as "user" | "assistant" | "system",
    content: m.content,
    timestamp: new Date(m.created_at).getTime(),
    streaming: false,
  }));
}

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
  if (target.kind === "planner" && target.task_id) return { kind: "planner", task_id: target.task_id };
  return target.kind === "manager" && target.id !== MANAGER_TARGET.id
    ? { kind: "manager", id: target.id }
    : { kind: "manager" };
}

// The Manager keeps the switchable session; each task's Planner has one fixed
// thread; a Worker or Reviewer has no chat, only its runs.
function sessionFor(target: ChatTargetInfo): string {
  if (target.kind === "planner") return `planner-${target.task_id}`;
  if (target.kind === "manager") return getOrCreateSessionId(target.id);
  return "";
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

// The default Manager keeps the original key; every other Manager has a pointer of its own.
const sessionKey = (managerId: string) =>
  managerId === "manager" ? STORAGE_SESSION_KEY : `${STORAGE_SESSION_KEY}:${managerId}`;
const localThreadsKey = (managerId: string) => `looper-chat-local-threads:${managerId}`;

function getOrCreateSessionId(managerId = "manager"): string {
  const key = sessionKey(managerId);
  const existing = localStorage.getItem(key) ?? (managerId === "manager" ? localStorage.getItem(LEGACY_SESSION_KEY) : null);
  if (existing) {
    localStorage.setItem(key, existing);
    return existing;
  }
  const id = newSessionId();
  localStorage.setItem(key, id);
  return id;
}

function newSessionId(): string {
  return `session-${randomId().slice(0, 8)}`;
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

/** The hash query for an open agent and read-only conversation; the Manager and no conversation are left out. */
function chatQuery(targetId: string, conversationId = ""): string {
  const query = new URLSearchParams();
  if (targetId && targetId !== MANAGER_TARGET.id) query.set("target", targetId);
  if (conversationId) query.set("conversation", conversationId);
  return query.toString();
}

/** `#chat?target=<agent id>&conversation=<session id>`: the agent open (the Manager when left out or unknown) and, with the
 * Manager, a Lark or Telegram conversation read-only. `query` is undefined while another page is shown: the chat stays
 * mounted, and keeps what it last read. */
export function ChatPage({ query, onQuery }: { query?: string; onQuery?: (query: string, replace?: boolean) => void }) {
  const seenQuery = useRef("");
  if (query !== undefined) seenQuery.current = query;
  const urlParams = new URLSearchParams(seenQuery.current);
  const wantTarget = urlParams.get("target") || MANAGER_TARGET.id;
  const wantConversation = urlParams.get("conversation") || "";
  const [sessionId, setSessionId] = useState(getOrCreateSessionId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [notice, setNotice] = useState("");
  const [staged, setStagedState] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  // The id of the message this page's own reply stream answers.
  const [ownId, setOwnId] = useState<string | null>(null);
  // A turn running on the server that this page does not stream (a reload, or a stream that ended early).
  const [serverTurn, setServerTurn] = useState<{ sid: string; ids: string[] } | null>(null);
  const [phase, setPhase] = useState("");
  const [roleConfigs, setRoleConfigs] = useState<RoleConfig[]>([]);
  const [targets, setTargets] = useState<ChatTargetInfo[]>([MANAGER_TARGET]);
  const [targetsLoaded, setTargetsLoaded] = useState(false);
  const [target, setTarget] = useState<ChatTargetInfo>(MANAGER_TARGET);
  const [agentDropdown, setAgentDropdown] = useState(false);
  // Tasks the user opened or closed in the agent list; others follow the default.
  const [taskOpen, setTaskOpen] = useState<Record<string, boolean>>({});
  const [plannerActivity, setPlannerActivity] = useState<ActivityEvent[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const turnRef = useRef(0); // the send in flight; an older stream must not touch a newer turn's state
  // A send belongs to the adopted target/session, even while a read-only or unresolved link hides its composer.
  // switchWebSession and applyTarget cancel it only when a different writable owner is actually adopted.
  // Leaving the page (the session ended, for one) stops the reply stream.
  useEffect(() => () => cleanupRef.current?.(), []);

  // Stopping a reply is a system setting (off by default); until it is read, there is no Stop.
  const [stopEnabled, setStopEnabled] = useState(false);
  useEffect(() => {
    let alive = true;
    fetchChatSettings().then((s) => alive && setStopEnabled(s.stop_enabled), () => {});
    return () => {
      alive = false;
    };
  }, []);
  // Whether the Manager's runtime can interrupt the reply to a message (its turn's `can_interrupt`, last read), by the
  // ids the turn answers; a runtime run per turn cannot. Another message's answer says nothing of this one: its turn
  // may switch the runtime first.
  const [interrupt, setInterrupt] = useState<{ ids: string[]; can: boolean } | null>(null);
  const readInterrupt = (turn: ChatTurn) => {
    const ids = [turn.message_id, turn.client_message_id].filter((id): id is string => !!id);
    setInterrupt({ ids, can: turn.can_interrupt });
  };
  const messagesRef = useRef(messages);
  messagesRef.current = messages;
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const dbLoadedRef = useRef(false);
  const stagedRef = useRef<string[]>([]);
  const sendingRef = useRef(false);
  sendingRef.current = sending;
  const sendRef = useRef<(text: string, newSessionProfileId?: string) => Promise<void>>(async () => {});
  const canSendRef = useRef(true);
  const pollRef = useRef<{ sid: string; ids: string[]; stopped: boolean; timer?: ReturnType<typeof setTimeout> } | null>(null);
  // The Manager's conversations on every channel, and the Lark or Telegram one open read-only (null: the web thread).
  const [conversations, setConversations] = useState<{ managerId: string; rows: ChatSession[] } | null>(null);
  const [conversationsError, setConversationsError] = useState("");
  const [profileRefresh, setProfileRefresh] = useState(0);
  const [newPanel, setNewPanel] = useState<ManagerSessionView | null>(null);
  const [newPanelOpen, setNewPanelOpen] = useState(false);
  const newSessionButtonRef = useRef<HTMLButtonElement>(null);
  // The new thread that replaced a stored one of another Manager, so the page says why it is empty.
  const [replacedThread, setReplacedThread] = useState<{ managerId: string; sessionId: string } | null>(null);
  // Its messages as last read, by session id, so another conversation's never show while one loads.
  const [viewingMessages, setViewingMessages] = useState<{ id: string; rows: DbChatMessage[] } | null>(null);
  // Below the md breakpoint the list takes the messages' place while it is open.
  const [listOpen, setListOpen] = useState(false);

  // The Manager and session shown now; a late answer is applied only when it was asked for these.
  const currentRef = useRef({ managerId: target.id, sessionId });
  currentRef.current = { managerId: target.id, sessionId };
  const localThreads = useRef(new Map<string, Set<string>>());
  const localThreadsFor = (managerId: string) => {
    let local = localThreads.current.get(managerId);
    if (!local) {
      let ids: unknown;
      try {
        ids = JSON.parse(localStorage.getItem(localThreadsKey(managerId)) ?? "[]");
      } catch {
        // Storage blocked or corrupt: keep this page's local threads in memory.
      }
      local = new Set(Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : []);
      localThreads.current.set(managerId, local);
    }
    return local;
  };
  const rememberLocalThread = (managerId: string, id: string) => {
    const local = localThreadsFor(managerId);
    local.add(id);
    try {
      localStorage.setItem(localThreadsKey(managerId), JSON.stringify([...local]));
    } catch {
      // Storage full or blocked: Back still works for this page's life.
    }
  };
  const isCurrentManager = (managerId: string) => managerId === currentRef.current.managerId;
  const currentManagerId = target.kind === "manager" ? target.id : "";
  // The web thread the Manager-scoped history read confirmed as this Manager's (its own or a new one): until then two
  // Managers' pointers may name the same thread, so nothing of it is shown, followed or watched.
  const [ownedThread, setOwnedThread] = useState<{ managerId: string; sessionId: string } | null>(null);
  const threadOwned =
    target.kind !== "manager" || (ownedThread?.managerId === target.id && ownedThread.sessionId === sessionId);

  const refreshConversations = useCallback((managerId: string, signal?: AbortSignal) => {
    fetchChatSessions(managerId, signal)
      .then((rows) => {
        if (signal?.aborted || managerId !== currentRef.current.managerId) return;
        setConversations({ managerId, rows });
        setConversationsError("");
      })
      .catch((err) => {
        if (signal?.aborted || (err instanceof Error && err.name === "AbortError")) return;
        if (managerId !== currentRef.current.managerId) return;
        setConversationsError(err instanceof Error ? err.message : String(err));
      });
  }, []);

  const refreshTargets = useCallback(() => {
    fetchChatTargets().then((rows) => {
      setTargets(rows);
      setTargetsLoaded(true);
    }).catch(() => {});
  }, []);

  useEffect(() => {
    fetchRoleConfigs().then(setRoleConfigs).catch(() => {});
    refreshTargets();
    // Keeps statuses current, and stops input once a viewed Planner ends.
    const timer = setInterval(refreshTargets, TARGETS_POLL_MS);
    return () => clearInterval(timer);
  }, [refreshTargets]);

  const managers = useMemo(() => {
    const rows = targets.filter((t) => t.kind === "manager");
    return rows.length > 0 ? rows : [MANAGER_TARGET];
  }, [targets]);
  const groups = useMemo(() => groupByTask(targets), [targets]);
  // The selected agent as last listed: its status and whether it takes messages.
  const current = targets.find((t) => t.id === target.id) ?? target;
  const taskGroup = groups.find((g) => g.planner.task_id === current.task_id);
  const isTaskOpen = (g: TaskGroup) =>
    taskOpen[g.planner.task_id ?? ""] ?? (g.planner.can_send || g.planner.task_id === current.task_id);

  const managerConfig = roleConfigs.find((c) => c.role === "manager");
  const targetTitle = (t: ChatTargetInfo) => {
    if (t.kind === "planner") return `Planner · ${truncate(t.name || t.requirement || t.task_id || "", 40)}`;
    if (t.kind === "manager") return t.name ?? (managerConfig?.display_name || "Manager");
    return `${stepAgentTitle(t)} · ${truncate(taskGroup?.planner.name || taskGroup?.planner.requirement || t.task_id || "", 32)}`;
  };
  const targetSubtitle = (t: ChatTargetInfo) =>
    t.kind === "manager"
      ? t.id === MANAGER_TARGET.id ? "global" : t.id
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
    const controller = new AbortController();
    const refresh = () => refreshConversations(currentManagerId, controller.signal);
    refresh();
    const timer = setInterval(refresh, TARGETS_POLL_MS);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [target.kind, currentManagerId, sending, refreshConversations]);
  const shownConversations = conversations?.managerId === currentManagerId ? conversations.rows : null;

  // A Lark or Telegram conversation is read-only here: its newest messages, re-read on the same cadence.
  // The conversation the address names is read by its id as well, as the list holds only the newest: a link to an
  // older one opens, and one open stays open when the list moves on.
  // `owner`: the other Manager it belongs to (the server's 409), so it is never opened as a new thread of this one.
  const [opened, setOpened] = useState<
    { managerId: string; id: string; session: ChatSession | null; error: string; owner?: ForeignConversationError } | null
  >(null);
  // The address's conversation is read and applied only under the agent the address names (an unknown one: the
  // Manager it falls back to), never under the one shown before the agents are listed.
  const targetSettled = wantTarget === target.id || (targetsLoaded && !targets.some((t) => t.id === wantTarget));
  useEffect(() => {
    if (target.kind !== "manager" || !wantConversation || !targetSettled) return;
    let alive = true;
    const controller = new AbortController();
    const managerId = currentManagerId;
    const fresh = () => alive && isCurrentManager(managerId);
    const load = () =>
      fetchChatSession(wantConversation, managerId, controller.signal).then(
        (session) => {
          if (!fresh()) return;
          const nonWeb = wantConversation.startsWith("planner-") || wantConversation.includes(":");
          if (!session && !nonWeb && wantConversation === currentRef.current.sessionId) {
            rememberLocalThread(managerId, wantConversation);
          }
          setOpened({
            managerId, id: wantConversation, session,
            error: !session && (nonWeb || !localThreadsFor(managerId).has(wantConversation))
              ? "Conversation not found." : "",
          });
        },
        (err) =>
          fresh() &&
          !(err instanceof Error && err.name === "AbortError") &&
          setOpened((prev) => err instanceof ForeignConversationError ? (
            { managerId, id: wantConversation, session: null, error: "", owner: err }
          ) : ({
            managerId,
            id: wantConversation,
            session: prev?.managerId === managerId && prev.id === wantConversation ? prev.session : null,
            error: err instanceof Error ? err.message : String(err),
          })),
      );
    load();
    const timer = setInterval(load, TARGETS_POLL_MS);
    return () => {
      alive = false;
      controller.abort();
      clearInterval(timer);
    };
  }, [target.kind, currentManagerId, wantConversation, targetSettled]);
  const openedHere = opened?.id === wantConversation && opened.managerId === currentManagerId ? opened : null;
  const knownConversation =
    target.kind === "manager" && wantConversation && targetSettled
      ? openedHere?.session ?? null
      : null;
  const viewing = knownConversation && knownConversation.channel !== "web" ? knownConversation : null;
  // Named by the address but not read yet: loading, the read failed (and it is tried again), or another Manager's.
  const conversationUnread =
    target.kind === "manager" && !!wantConversation && wantConversation !== sessionId &&
    (!targetSettled || !knownConversation && (!openedHere || !!openedHere.error || !!openedHere.owner));
  const canSend = current.can_send && !viewing && !conversationUnread;
  // Queue draining must honor the same guards as sending, or hidden composers lose their queued text.
  canSendRef.current = canSend;
  const conversationError = conversationUnread ? openedHere?.error ?? "" : "";
  const conversationOwner = conversationUnread ? openedHere?.owner ?? null : null;
  const viewingId = viewing?.session_id;
  useEffect(() => {
    if (!viewingId) return;
    let alive = true;
    const controller = new AbortController();
    const managerId = currentRef.current.managerId;
    const fresh = () => alive && isCurrentManager(managerId);
    const load = () =>
      fetchChatMessages(viewingId, controller.signal, managerId)
        .then((rows) => fresh() && setViewingMessages({ id: viewingId, rows }))
        .catch((err) => {
          if (err instanceof Error && err.name === "AbortError") return;
          if (fresh()) setViewingMessages((prev) => (prev?.id === viewingId ? prev : { id: viewingId, rows: [] }));
        });
    load();
    const timer = setInterval(load, TARGETS_POLL_MS);
    return () => {
      alive = false;
      controller.abort();
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewingId]);

  // The web thread to continue; the address names it too, so it is kept by a link, a refresh, Back and Forward.
  const switchWebSession = (id: string, managerId: string = currentRef.current.managerId) => {
    cleanupRef.current?.();
    cleanupRef.current = null;
    localStorage.setItem(sessionKey(managerId), id);
    currentRef.current = { managerId, sessionId: id };
    setSessionId(id);
    setInput("");
    setMessages([]);
    setSending(false);
    setPhase("");
  };
  // The conversation in the address that is already applied (or chosen here), so the address is applied once.
  const appliedConversation = useRef("");
  // Pin the shown thread to this history entry before New conversation pushes the next one.
  useEffect(() => {
    if (query === undefined || target.kind !== "manager" || !targetSettled || wantConversation || !threadOwned) return;
    onQuery?.(chatQuery(target.id, sessionId), true);
  }, [query, target.kind, target.id, targetSettled, wantConversation, threadOwned, sessionId, onQuery]);
  const openConversation = (session: ChatSession, fromManagerId?: string) => {
    // A click queued on a row of the Manager shown before a switch does nothing.
    if (fromManagerId !== undefined && fromManagerId !== currentRef.current.managerId) return;
    setListOpen(false);
    onQuery?.(chatQuery(target.id, session.session_id));
  };
  // Adopt an existing web thread, or return to an unsaved thread created here (Back/Forward). Empty arbitrary ids
  // cannot take over the stored composer owner.
  useEffect(() => {
    if (!wantConversation) appliedConversation.current = "";
    if (target.kind !== "manager" || !wantConversation || appliedConversation.current === wantConversation) return;
    if (conversationUnread) return;
    appliedConversation.current = wantConversation;
    if (!viewing && wantConversation !== sessionId) switchWebSession(wantConversation);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target.kind, wantConversation, conversationUnread, viewing]);

  const selectTarget = (next: ChatTargetInfo) => {
    setAgentDropdown(false);
    setListOpen(false);
    onQuery?.(chatQuery(next.id));
  };

  // The agent the address names, whether it was chosen here or the address changed (a link, Back, a refresh). An
  // unknown one is the Manager, once the agents are listed.
  const applyTarget = (next: ChatTargetInfo) => {
    if (next.id === target.id) return;
    cleanupRef.current?.();
    cleanupRef.current = null;
    stopPoll();
    setServerTurn(null);
    setSending(false);
    setPhase("");
    setInput("");
    setMessages([]);
    if (next.id !== target.id) {
      setConversations(null);
      setOpened(null);
      setViewingMessages(null);
      appliedConversation.current = "";
      // The panel shows the previous Manager's Profile, and confirming it would reset this one's session.
      newPanelAbort.current?.abort();
      setNewPanelOpen(false);
      setNewPanel(null);
    }
    const nextSession = sessionFor(next);
    currentRef.current = { managerId: next.id, sessionId: nextSession };
    setTarget(next);
    setSessionId(nextSession);
  };
  useEffect(() => {
    if (query === undefined || wantTarget === target.id) return;
    const next = wantTarget === MANAGER_TARGET.id ? MANAGER_TARGET : targets.find((t) => t.id === wantTarget);
    if (next) applyTarget(next);
    else if (targetsLoaded) applyTarget(MANAGER_TARGET);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, wantTarget, target.id, targets, targetsLoaded]);

  const scrollToBottom = useCallback(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, []);

  useEffect(scrollToBottom, [messages, scrollToBottom]);
  // A read-only conversation opens at its newest message, and follows when one arrives (not on every re-read).
  const viewingShown = viewingMessages ? `${viewingMessages.id}:${viewingMessages.rows.length}` : "";
  useEffect(scrollToBottom, [viewingShown, scrollToBottom]);

  // Load messages from DB on mount (or a session or Manager change). A Manager's thread is read Manager-scoped, which
  // also confirms it is this Manager's before it is shown, followed or watched.
  useEffect(() => {
    dbLoadedRef.current = false;
    if (!sessionId) {
      setMessages([]);
      dbLoadedRef.current = true;
      return;
    }
    let alive = true;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const requested = { managerId: currentRef.current.managerId, sessionId };
    const fresh = () =>
      alive &&
      requested.managerId === currentRef.current.managerId &&
      requested.sessionId === currentRef.current.sessionId;
    const owner = target.kind === "manager" ? requested.managerId : undefined;
    const load = () => fetchChatMessages(sessionId, controller.signal, owner).then((dbMsgs) => {
      if (!fresh()) return;
      if (owner) setOwnedThread(requested);
      if (dbMsgs.length > 0) {
        setMessages(fromDb(dbMsgs));
      } else {
        const local = loadMessages(requested.sessionId);
        if (local.length > 0) setMessages(local);
      }
      dbLoadedRef.current = true;
    }).catch((err) => {
      if (!fresh() || (err instanceof Error && err.name === "AbortError")) return;
      if (err instanceof ForeignConversationError) {
        // The stored thread is another Manager's: this Manager starts a new one instead of continuing it.
        const fresh = newSessionId();
        setReplacedThread({ managerId: requested.managerId, sessionId: fresh });
        switchWebSession(fresh);
        return;
      }
      // Whose thread it is stays unknown, so a Manager's cached copy is not shown; the check is tried again.
      if (owner) {
        retry = setTimeout(load, TURN_POLL_MS);
        return;
      }
      const local = loadMessages(requested.sessionId);
      if (local.length > 0) setMessages(local);
      dbLoadedRef.current = true;
    });
    load();
    return () => {
      alive = false;
      clearTimeout(retry);
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, target.id]);

  // What the Manager's Claude says on its own (after background work it started
  // ends) answers no message, so no message's stream carries it: it arrives on
  // the open chat's own stream and shows as a message of its own.
  useEffect(() => {
    if (target.kind !== "manager" || !sessionId || !threadOwned) return;
    const managerId = target.id;
    return watchChat(sessionId, (m) => {
      // An event that arrives after a switch is another Manager's or another thread's.
      if (managerId !== currentRef.current.managerId || sessionId !== currentRef.current.sessionId) return;
      setMessages((prev) =>
        prev.some((p) => p.id === m.message_id)
          ? prev.map((p) =>
              // The saved reply replaces the partial bubble this page streamed under the same id.
              p.id === m.message_id && p.role === "assistant" ? { ...p, content: m.content, streaming: false } : p,
            )
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
      );
    }, managerId);
  }, [sessionId, target.kind, target.id, threadOwned]);

  // Keep localStorage as cache
  useEffect(() => {
    if (sessionId && messages.length > 0) {
      saveMessages(sessionId, messages);
    }
  }, [messages, sessionId]);

  // The queued messages of the open thread, mirrored to sessionStorage so a reload keeps them.
  const setStaged = (next: string[]) => {
    stagedRef.current = next;
    setStagedState(next);
    writeStaged(sessionIdRef.current, next);
  };

  // Queued messages that will not be sent go back to the draft, ahead of what is typed there.
  const pullStagedToDraft = (extra: string[] = []) => {
    const list = [...extra, ...stagedRef.current];
    if (list.length === 0) return;
    setStaged([]);
    setInput((draft) => pullBack(list, draft));
  };

  const stopPoll = () => {
    const poll = pollRef.current;
    if (!poll) return;
    poll.stopped = true;
    clearTimeout(poll.timer);
    pollRef.current = null;
  };
  useEffect(() => stopPoll, []);

  // Waits for a turn this page does not stream to end, then re-reads the thread and returns the queue to the draft.
  const followTurn = (sid: string, ids: string[]) => {
    const running = pollRef.current;
    if (running && running.sid === sid && ids.some((id) => running.ids.includes(id))) return;
    stopPoll();
    const poll: NonNullable<typeof pollRef.current> = { sid, ids, stopped: false };
    pollRef.current = poll;
    setServerTurn({ sid, ids });
    const managerId = currentRef.current.managerId;
    const owner = target.kind === "manager" ? managerId : undefined;
    const tick = async () => {
      let turn: Awaited<ReturnType<typeof fetchChatTurn>> | undefined;
      try {
        turn = await fetchChatTurn(sid);
      } catch {
        turn = undefined;
      }
      if (poll.stopped) return;
      if (turn && turnAnswers(turn, ids)) readInterrupt(turn);
      if (turn === undefined || turnAnswers(turn, ids)) {
        poll.timer = setTimeout(tick, TURN_POLL_MS);
        return;
      }
      // Still replying while the history is re-read: a message sent now stops this poll, and its read is dropped.
      const rows = await fetchChatMessages(sid, undefined, owner).catch(() => []);
      if (poll.stopped || sid !== sessionIdRef.current || managerId !== currentRef.current.managerId) return;
      pollRef.current = null;
      setServerTurn(null);
      if (rows.length > 0) setMessages(fromDb(rows));
      pullStagedToDraft();
    };
    poll.timer = setTimeout(tick, TURN_POLL_MS);
  };

  // On mount, a thread change and a return to the tab or web conversation: is the Manager still answering it?
  const syncTurn = async (sid: string, isCurrent: () => boolean) => {
    if (sendingRef.current) return;
    if (target.kind !== "manager") {
      pullStagedToDraft();
      return;
    }
    const managerId = currentRef.current.managerId;
    let turn: Awaited<ReturnType<typeof fetchChatTurn>>;
    try {
      turn = await fetchChatTurn(sid);
    } catch {
      return;
    }
    if (!isCurrent() || sid !== sessionIdRef.current || managerId !== currentRef.current.managerId || sendingRef.current) return;
    if (turn) {
      readInterrupt(turn);
      followTurn(sid, turn.client_message_id ? [turn.message_id, turn.client_message_id] : [turn.message_id]);
    } else {
      stopPoll();
      setServerTurn(null);
      pullStagedToDraft();
    }
  };
  const syncTurnRef = useRef(syncTurn);
  syncTurnRef.current = syncTurn;

  // Returning to the writable thread resumes polling if no live send/stream is already following its turn.
  const threadOpen = !viewing && !conversationUnread && (!wantConversation || wantConversation === sessionId);
  useEffect(() => {
    stagedRef.current = readStaged(sessionId);
    setStagedState(stagedRef.current);
    setNotice("");
    stopPoll();
    setServerTurn(null);
    // A Manager's turn is followed only on a thread confirmed as its own.
    if (!threadOwned || !threadOpen) return;
    let alive = true;
    const isCurrent = () => alive;
    void syncTurnRef.current(sessionId, isCurrent);
    const onVisible = () => {
      if (document.visibilityState === "visible") void syncTurnRef.current(sessionId, isCurrent);
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [sessionId, target.kind, target.id, threadOwned, threadOpen]);

  const serverTurnHere = serverTurn && serverTurn.sid === sessionId ? serverTurn : null;
  const replying = sending || serverTurnHere !== null;
  const runningId = sending ? ownId : (serverTurnHere ? serverTurnHere.ids[serverTurnHere.ids.length - 1] : null);
  const stoppable = stopEnabled && target.kind === "manager" && runningId !== null;
  // Not read yet for the running message: the server decides (it refuses a stop its runtime cannot make).
  const interruptible = !runningId || !interrupt?.ids.includes(runningId) || interrupt.can;
  const canStop = stoppable && interruptible;
  // The page's own reply: read whether its runtime can interrupt it, again until it ends, since the turn may switch
  // the runtime after its first read (the server's turns are read as they are followed).
  useEffect(() => {
    if (!sending || !ownId || !stopEnabled || target.kind !== "manager") return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const read = async () => {
      const turn = await fetchChatTurn(sessionId).catch(() => null);
      if (!alive) return;
      if (turn && turnAnswers(turn, [ownId])) readInterrupt(turn);
      timer = setTimeout(read, TURN_POLL_MS);
    };
    void read();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [sending, ownId, stopEnabled, target.kind, sessionId]);

  const handleStop = async () => {
    if (!runningId) return;
    if (!stopEnabled) return setNotice(STOP_DISABLED_NOTICE); // a typed /stop: nothing is sent
    if (!interruptible) return setNotice(CANNOT_INTERRUPT_NOTICE);
    try {
      const { state } = await stopChatReply(runningId);
      if (state === "disabled") setNotice(STOP_DISABLED_NOTICE);
      else if (state === "other_message") setNotice(OTHER_MESSAGE_NOTICE);
      else if (state === "cannot_interrupt") {
        setInterrupt({ ids: [runningId], can: false });
        setNotice(CANNOT_INTERRUPT_NOTICE);
      }
    } catch (err) {
      setNotice(err instanceof Error ? err.message : String(err));
    }
  };

  const handleNewChat = () => {
    const newId = newSessionId();
    if (threadOwned && messages.length === 0) rememberLocalThread(target.id, sessionId);
    rememberLocalThread(target.id, newId);
    appliedConversation.current = newId;
    onQuery?.(chatQuery(target.id, newId));
    switchWebSession(newId);
    setInput("");
  };

  const closeNewPanel = () => {
    setNewPanelOpen(false);
    newSessionButtonRef.current?.focus();
  };

  const newPanelAbort = useRef<AbortController | null>(null);
  const toggleNewPanel = () => {
    if (newPanelOpen) return closeNewPanel();
    setNewPanelOpen(true);
    setNewPanel(null);
    const requested = { managerId: target.id, sessionId };
    newPanelAbort.current?.abort();
    const controller = new AbortController();
    newPanelAbort.current = controller;
    const fresh = () =>
      !controller.signal.aborted &&
      requested.managerId === currentRef.current.managerId &&
      requested.sessionId === currentRef.current.sessionId;
    fetchManagerSession(target.id, controller.signal).then((view) => fresh() && setNewPanel(view)).catch((err) => {
      if (!fresh() || (err instanceof Error && err.name === "AbortError")) return;
      setNewPanel({
      session: { active: false, runtime: null, profile_id: null }, selected: null,
      effective: { profile: null, source: "session" }, problem: null, follow_settings: null, choices: [],
      });
    });
  };

  const handleSend = async (text: string, newSessionProfileId?: string) => {
    if (!text.trim() || !canSend) return;

    const sid = sessionId;
    const managerId = target.id;
    cleanupRef.current?.();
    const turn = ++turnRef.current;
    const controller = new AbortController();
    const current_ = () => turn === turnRef.current && !controller.signal.aborted &&
      managerId === currentRef.current.managerId && sid === currentRef.current.sessionId;
    let close: (() => void) | undefined;
    const cancel = () => {
      controller.abort();
      close?.();
      if (cleanupRef.current === cancel) cleanupRef.current = null;
    };
    // Installed before awaiting sendChat: navigation owns cancellation throughout the send's lifetime.
    cleanupRef.current = cancel;
    stopPoll();
    setServerTurn(null);
    setNotice("");
    setSending(true);
    const changesProfile = text.toLowerCase() === NEW_SESSION_COMMAND || /^\/profile(\s|$)/i.test(text);
    setPhase("");
    let finalSeen = false;

    const userMsg: ChatMessage = {
      id: randomId(),
      role: "user",
      content: text,
      timestamp: Date.now(),
    };
    setOwnId(userMsg.id);
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
      const { correlation_id } = await sendChat(text, sid, userMsg.id, sendTargetOf(target), {
        newSessionProfileId, signal: controller.signal,
      });
      localThreads.current.get(managerId)?.delete(sid); // A successful send makes this a persisted thread.
      if (!current_()) return;

      close = streamChatReply(
        correlation_id,
        (event: SSEEvent) => {
          if (!current_()) return;
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
              finalSeen = true;
              const finalText = (event.payload as any).text || "";
              const outcome: string = (event.payload as any).outcome || "answered";
              setMessages((prev) =>
                prev.map((m) =>
                  m.id === assistantId
                    ? {
                        ...m,
                        content: finalText || m.content || "Done.",
                        streaming: false,
                        outcome: outcome === "stopped" ? "stopped" : undefined,
                      }
                    : m
                )
              );
              setSending(false);
              setPhase("");
              if (changesProfile) setProfileRefresh((n) => n + 1);
              const list = stagedRef.current;
              if (list.length > 0) {
                // Cleared first, so the batch can never go out twice.
                setStaged([]);
                if (outcome === "answered" && canSendRef.current) void sendRef.current(joinStaged(list));
                else setInput((draft) => pullBack(list, draft));
              }
              break;
            }
            case "agent.phase":
              setPhase((event.payload as any).label || "");
              break;
            case "turn.completed":
            case "turn.failed":
              // WebChannelAdapter saves the reply to the captured send session, including after navigation.
              setMessages((prev) => current_()
                ? prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m)) : prev);
              setSending(false);
              setPhase("");
              break;
          }
        },
        () => {
          if (!current_()) return;
          setSending(false);
          setPhase("");
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, streaming: false } : m))
          );
          if (finalSeen) return;
          // The stream ended with no final answer (its deadline, say): the turn may still run on the server.
          if (target.kind === "manager") followTurn(sid, [userMsg.id]);
          else pullStagedToDraft();
        }
      );
      // A synchronous stream callback can already have started the next staged send.
      if (!current_()) close();
    } catch (err) {
      if (!current_()) return;
      // fetch() rejects with a TypeError when the server is unreachable; other
      // errors carry the server's reason, e.g. the task's Planner has stopped.
      const reason =
        err instanceof TypeError || !(err instanceof Error)
          ? "Failed to connect. Is the server running?"
          : err.message;
      setSending(false);
      if (text.toLowerCase() === NEW_SESSION_COMMAND) {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, content: reason, streaming: false } : m)),
        );
      } else {
        // Nothing is retried: the text returns to the draft with the reason.
        setMessages((prev) => prev.filter((m) => m.id !== userMsg.id && m.id !== assistantId));
        pullStagedToDraft([text]);
        setNotice(reason);
      }
      if (target.kind === "planner") refreshTargets();
    }
  };

  sendRef.current = handleSend;

  const managerView = target.kind === "manager";
  const conversationList = (
    <ConversationList
      sessions={shownConversations}
      error={conversationsError}
      selected={viewing?.session_id ?? sessionId}
      onSelect={(session) => openConversation(session, currentManagerId)}
      onRefresh={() => refreshConversations(currentManagerId)}
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
                {managers.map((m) => (
                  <AgentRow
                    key={m.id}
                    title={targetTitle(m)}
                    subtitle={targetSubtitle(m)}
                    selected={target.id === m.id}
                    onSelect={() => selectTarget(m)}
                  />
                ))}
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
            {target.kind === "manager" && !viewing && <ManagerSessionProfile refreshKey={profileRefresh} managerId={target.id} />}
            {target.kind === "manager" && !viewing && (
              <button
                type="button"
                ref={newSessionButtonRef}
                onClick={toggleNewPanel}
                disabled={sending}
                className="flex h-7 items-center gap-1.5 rounded-md border border-border px-2 text-xs text-muted-foreground hover:text-foreground hover:bg-accent transition-colors disabled:opacity-50"
                title="Start a new Manager session, optionally on another Agent Profile"
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

      {newPanelOpen && target.kind === "manager" && !viewing && (
        <NewSessionPanel
          settingsName={
            newPanel && newPanel.effective.source !== "session" && newPanel.effective.profile
              ? sessionProfileLabel(newPanel.effective.profile)
              : null
          }
          choices={newPanel?.choices ?? []}
          busy={sending}
          onStart={(id) => {
            closeNewPanel();
            handleSend(NEW_SESSION_COMMAND, id ?? undefined);
          }}
          onCancel={closeNewPanel}
        />
      )}

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
          <div data-viewing={viewing.session_id} className="contents">
          <ChannelMessages
            session={shownConversations?.find((c) => c.session_id === viewing.session_id) ?? viewing}
            messages={viewingMessages?.id === viewing.session_id ? viewingMessages.rows : null}
          />
          </div>
        )}

        {conversationUnread &&
          (conversationOwner ? (
            <div role="alert" className="py-10 text-center text-sm">
              <p className="text-destructive">This is another Manager's conversation: {conversationOwner.message}</p>
              <div className="mt-3 flex justify-center gap-2">
                {targets.some((t) => t.kind === "manager" && t.id === conversationOwner.managerId) && (
                  <button
                    type="button"
                    onClick={() => onQuery?.(chatQuery(conversationOwner.managerId, wantConversation))}
                    className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                  >
                    Open it there
                  </button>
                )}
                <button type="button" onClick={handleNewChat} className="rounded-md border border-border px-3 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-accent transition-colors">
                  New conversation here
                </button>
              </div>
            </div>
          ) : conversationError ? (
            <p role="alert" className="py-10 text-center text-sm text-destructive">
              Could not load the conversation: {conversationError}
            </p>
          ) : (
            <p role="status" className="flex items-center justify-center gap-2 py-10 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              Loading the conversation…
            </p>
          ))}

        {replacedThread?.managerId === target.id && replacedThread.sessionId === sessionId && !viewing &&
          !conversationUnread && (
          <p role="status" className="text-center text-xs text-muted-foreground">
            The saved conversation belongs to another Manager, so this is a new conversation.
          </p>
        )}

        {messages.length === 0 && target.kind === "manager" && !viewing && !conversationUnread && (
          <div className="flex h-full items-center justify-center">
            <div className="text-center">
              <Bot className="mx-auto h-12 w-12 text-muted-foreground/50" />
              <p className="mt-3 text-sm text-muted-foreground">Send a message to start</p>
            </div>
          </div>
        )}

        {!viewing && !conversationUnread && messages.map((msg) => (
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
              {msg.outcome === "stopped" && (
                <p className="mt-1.5 flex items-center gap-1 text-xs text-muted-foreground">
                  <Square className="h-3 w-3 fill-current" aria-hidden="true" />
                  Stopped
                </p>
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
        ) : conversationUnread ? null : !current.can_send ? (
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
        <ChatComposer
          key={sessionId}
          draft={input}
          onDraft={setInput}
          staged={staged}
          onStaged={setStaged}
          history={historyFrom(messages)}
          replying={replying}
          canStop={canStop}
          stopRefusal={stoppable && !interruptible ? CANNOT_INTERRUPT_NOTICE : undefined}
          onSend={(text) => {
            setInput("");
            void handleSend(text);
          }}
          onStop={() => void handleStop()}
          notice={notice || undefined}
        />
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
