import { useEffect, useRef, useState } from "react";
import type { WorkConversationProps, WorkPaneMessage } from "@/components/work-conversation";
import {
  fetchChatMessages, fetchChatSettings, fetchChatTurn, ForeignConversationError,
  stopChatReply, watchChat, type ChatTurn, type DbChatMessage,
} from "@/lib/api";
import { historyFrom, joinStaged, pullBack, turnAnswers } from "@/lib/composer";
import { randomId } from "@/lib/ids";
import { useWorkConversationState } from "@/lib/use-work-conversation-state";
import { useReplyStream } from "@/lib/use-reply-stream";

const POINTER = "looper-chat-session-id";
const POLL_MS = 3000;
const STOP_DISABLED = "停止回复未开启（设置 → 对话）。";
const OTHER_MESSAGE = "管家正在回答另一条消息，本条尚未开始。";
const CANNOT_INTERRUPT = "此运行时无法中断回复。";
const FOREIGN = "保存的对话属于另一位管家，已新建对话。";
const newThread = () => `session-${randomId().slice(0, 8)}`;

// Same default pointer and creation policy as Chat; Work also tolerates blocked storage.
function currentThread(fallback?: string): string {
  try {
    const id = localStorage.getItem(POINTER) || fallback || newThread();
    localStorage.setItem(POINTER, id);
    return id;
  } catch {
    return fallback || newThread();
  }
}
function merge(rows: WorkPaneMessage[], saved: DbChatMessage[]): WorkPaneMessage[] {
  const next = new Map(rows.map(row => [row.id, row]));
  for (const row of saved) next.set(row.message_id, {
    id: row.message_id, role: row.role, content: row.content, created_at: row.created_at, streaming: false,
    outcome: next.get(row.message_id)?.outcome,
  });
  return [...next.values()];
}

interface Thread {
  sid: string;
  owned: boolean;
  history: WorkConversationProps["history"];
  error: string;
  messages: WorkPaneMessage[];
  saved: Set<string>;
  draft: string;
  staged: string[];
  sending: boolean;
  ownId: string | null;
  serverTurn: string[] | null;
  interrupt: { ids: string[]; can: boolean } | null;
  phase: string;
  notice: string;
}
function empty(sid: string): Thread {
  return {
    sid, owned: false, history: "loading", error: "", messages: [], saved: new Set(),
    draft: "", staged: [], sending: false, ownId: null,
    serverTurn: null, interrupt: null, phase: "", notice: "",
  };
}

/** Default Manager only. Navigation cancels browser clients, never the server turn. */
export function useWorkManagerConversation(): WorkConversationProps {
  const [sid, setSid] = useState(() => currentThread());
  const conversation = useWorkConversationState({ kind: "manager", managerId: "manager", sessionId: sid });
  const { store, key: cacheKey } = conversation;
  const confirmed = useRef("");
  const sidRef = useRef(sid);
  if (sidRef.current !== sid) confirmed.current = "";
  sidRef.current = sid;
  const [retry, setRetry] = useState(0);
  const [replaced, setReplaced] = useState("");
  const [stopEnabled, setStopEnabled] = useState(false);
  const [view, setView] = useState(() => empty(sid));
  const active = useRef<{
    thread: Thread; publish: () => void; send: (text: string) => void; stop: () => Promise<void>;
  } | null>(null);
  const stopEnabledRef = useRef(stopEnabled);
  stopEnabledRef.current = stopEnabled;
  const reply = useReplyStream(() => ({ id: "manager", threadId: sidRef.current }));
  const replyRef = useRef(reply);
  replyRef.current = reply;

  useEffect(() => {
    let alive = true;
    fetchChatSettings().then(settings => { if (alive) setStopEnabled(settings.stop_enabled); }).catch(() => {});
    const sync = () => setSid(previous => currentThread(previous));
    const storage = (event: StorageEvent) => { if (event.key === POINTER || event.key === null) sync(); };
    const visible = () => { if (document.visibilityState === "visible") sync(); };
    window.addEventListener("storage", storage);
    document.addEventListener("visibilitychange", visible);
    return () => {
      alive = false;
      window.removeEventListener("storage", storage);
      document.removeEventListener("visibilitychange", visible);
    };
  }, []);

  useEffect(() => {
    const thread = empty(sid);
    const composer = store.read(cacheKey);
    thread.draft = composer.draft; thread.staged = [...composer.staged];
    if (confirmed.current === sid) {
      thread.owned = true;
      thread.messages = store.cached(cacheKey)?.messages ?? [];
      thread.history = thread.messages.length ? "ready" : "empty";
    }
    const controller = new AbortController();
    let alive = true;
    let closeWatch: (() => void) | undefined;
    let poll: { ids: string[]; stopped: boolean; timer?: ReturnType<typeof setTimeout> } | null = null;
    let capabilityTimer: ReturnType<typeof setTimeout> | undefined;
    const fresh = () => alive && sidRef.current === sid;
    const publish = () => {
      if (!fresh()) return;
      if (thread.owned) {
        store.cache(cacheKey, { messages: thread.messages });
        const history = historyFrom(thread.messages);
        if (history.length) conversation.rememberHistory(history);
      }
      setView({ ...thread });
    };
    const setDraft = (draft: string) => { thread.draft = draft; store.patch(cacheKey, { draft }); };
    const setStaged = (staged: string[]) => {
      thread.staged = staged;
      store.patch(cacheKey, { staged: [...staged] });
    };
    const pullStaged = (extra: string[] = []) => {
      const items = [...extra, ...thread.staged];
      if (items.length) { setDraft(pullBack(items, thread.draft)); setStaged([]); }
    };
    const readInterrupt = (turn: ChatTurn) => {
      thread.interrupt = { ids: [turn.message_id, turn.client_message_id].filter((id): id is string => !!id), can: turn.can_interrupt };
    };
    const stopPoll = () => {
      if (poll) { poll.stopped = true; clearTimeout(poll.timer); poll = null; }
    };
    const saved = (rows: DbChatMessage[]) => {
      thread.messages = merge(thread.messages, rows);
      for (const row of rows) thread.saved.add(row.message_id);
    };
    // Chat's followTurn policy: keep the queue while the same turn runs. On
    // completion re-read history and return it to the draft, never auto-send it.
    const followTurn = (ids: string[]) => {
      if (poll && ids.some(id => poll!.ids.includes(id))) return;
      stopPoll();
      const running: NonNullable<typeof poll> = { ids, stopped: false };
      poll = running;
      thread.serverTurn = ids;
      const tick = async () => {
        const turn = await fetchChatTurn(sid).catch(() => undefined);
        if (!fresh() || running.stopped) return;
        if (turn && turnAnswers(turn, ids)) readInterrupt(turn);
        if (turn === undefined || turnAnswers(turn, ids)) {
          publish(); running.timer = setTimeout(tick, POLL_MS); return;
        }
        const rows = await fetchChatMessages(sid, controller.signal, "manager").catch(() => []);
        if (!fresh() || running.stopped) return;
        poll = null; thread.serverTurn = null; saved(rows); pullStaged(); publish();
      };
      running.timer = setTimeout(tick, POLL_MS);
    };
    const syncTurn = async () => {
      if (!thread.owned || thread.sending) return;
      const turn = await fetchChatTurn(sid).catch(() => undefined);
      if (!fresh() || thread.sending || turn === undefined) return;
      if (turn) {
        readInterrupt(turn);
        followTurn(turn.client_message_id ? [turn.message_id, turn.client_message_id] : [turn.message_id]);
      } else { stopPoll(); thread.serverTurn = null; pullStaged(); }
      publish();
    };
    const runningId = () => thread.sending ? thread.ownId : thread.serverTurn?.at(-1) ?? null;
    const interruptible = (id: string) => !thread.interrupt?.ids.includes(id) || thread.interrupt.can;
    const stop = async () => {
      const id = runningId();
      if (!fresh() || !thread.owned || !id) return;
      if (!stopEnabledRef.current) { thread.notice = STOP_DISABLED; publish(); return; }
      if (!interruptible(id)) { thread.notice = CANNOT_INTERRUPT; publish(); return; }
      try {
        const result = await stopChatReply(id);
        if (!fresh()) return;
        if (result.state === "disabled") thread.notice = STOP_DISABLED;
        else if (result.state === "other_message") thread.notice = OTHER_MESSAGE;
        else if (result.state === "cannot_interrupt") {
          thread.interrupt = { ids: [id], can: false }; thread.notice = CANNOT_INTERRUPT;
        }
      } catch (error) { if (fresh()) thread.notice = error instanceof Error ? error.message : String(error); }
      publish();
    };
    const send = (text: string) => {
      if (!fresh() || !thread.owned || !text.trim()) return;
      replyRef.current.cancel(); stopPoll(); clearTimeout(capabilityTimer);
      thread.serverTurn = null; thread.notice = ""; thread.sending = true; thread.phase = "";
      const id = randomId(); thread.ownId = id;
      const assistantId = `${id}:reply`;
      thread.messages = [...thread.messages,
        { id, role: "user", content: text }, { id: assistantId, role: "assistant", content: "", streaming: true }];
      let finalSeen = false;
      const isCurrent = { current: () => false };
      const current = () => fresh() && isCurrent.current();
      const updateReply = (update: (message: WorkPaneMessage) => WorkPaneMessage) => {
        thread.messages = thread.messages.map(message => message.id === assistantId && !thread.saved.has(assistantId) ? update(message) : message);
      };
      const readCapability = async () => {
        if (!current() || !thread.sending) return;
        if (stopEnabledRef.current) {
          const turn = await fetchChatTurn(sid).catch(() => null);
          if (!current() || !thread.sending) return;
          if (turn && turnAnswers(turn, [id])) { readInterrupt(turn); publish(); }
        }
        capabilityTimer = setTimeout(readCapability, POLL_MS);
      };
      replyRef.current.start({
        target: { kind: "manager" }, ownerId: "manager", threadId: sid, messageId: id, text, isCurrent,
        onEvent: event => {
          if (!current()) return;
          if (event.kind === "answer.delta") updateReply(message => ({ ...message, content: message.content + String(event.payload.text || "") }));
          else if (event.kind === "agent.phase") thread.phase = String(event.payload.label || "");
          else if (event.kind === "answer.final") {
            finalSeen = true;
            const outcome = String(event.payload.outcome || "answered");
            updateReply(message => ({ ...message, content: String(event.payload.text || message.content || "已完成。"),
              streaming: false, outcome: outcome === "stopped" ? "stopped" : undefined }));
            thread.sending = false; thread.phase = ""; clearTimeout(capabilityTimer);
            const staged = thread.staged;
            if (staged.length) {
              if (outcome === "answered") { setStaged([]); send(joinStaged(staged)); }
              else pullStaged();
            }
          } else if (event.kind === "turn.completed" || event.kind === "turn.failed") {
            updateReply(message => ({ ...message, streaming: false })); thread.sending = false; thread.phase = "";
          }
          publish();
        },
        onDone: () => {
          if (!current()) return;
          thread.sending = false; thread.phase = ""; clearTimeout(capabilityTimer);
          updateReply(message => ({ ...message, streaming: false }));
          if (!finalSeen) followTurn([id]);
          publish();
        },
        onError: error => {
          if (!current()) return;
          const reason = error instanceof TypeError || !(error instanceof Error) ? "连接失败，请检查服务器是否运行。" : error.message;
          thread.sending = false; clearTimeout(capabilityTimer);
          if (text.toLowerCase() === "/new") updateReply(message => ({ ...message, content: reason, streaming: false }));
          else {
            thread.messages = thread.messages.filter(message => message.id !== id && message.id !== assistantId);
            pullStaged([text]); thread.notice = reason;
          }
          publish();
        },
      });
      void readCapability(); publish();
    };
    active.current = { thread, publish, send, stop };
    publish();
    fetchChatMessages(sid, controller.signal, "manager").then(rows => {
      if (!fresh()) return;
      if (!Array.isArray(rows)) throw new Error("服务器返回了无效的对话消息。");
      if (!thread.owned) thread.messages = store.cached(cacheKey)?.messages ?? [];
      saved(rows); thread.owned = true; confirmed.current = sid; thread.history = rows.length ? "ready" : "empty"; publish();
      closeWatch = watchChat(sid, message => { if (fresh()) { saved([message]); publish(); } }, "manager");
      void syncTurn();
    }).catch(error => {
      if (!fresh()) return;
      if (error instanceof ForeignConversationError) {
        confirmed.current = "";
        thread.owned = false; thread.messages = []; publish();
        const next = newThread();
        try { localStorage.setItem(POINTER, next); } catch { /* Keep the replacement in memory. */ }
        setReplaced(next); setSid(next);
      } else {
        if (!thread.owned) thread.messages = [];
        thread.history = "error";
        thread.error = `${thread.owned ? "刷新失败：" : ""}${error instanceof Error ? error.message : String(error)}`; publish();
      }
    });
    const visible = () => { if (document.visibilityState === "visible") void syncTurn(); };
    document.addEventListener("visibilitychange", visible);
    return () => {
      alive = false; controller.abort(); closeWatch?.(); replyRef.current.cancel(); stopPoll(); clearTimeout(capabilityTimer);
      document.removeEventListener("visibilitychange", visible);
      if (active.current?.thread === thread) active.current = null;
    };
  }, [sid, retry]);

  const shown = view.sid === sid ? view : empty(sid);
  const owner = active.current?.thread.sid === sid ? active.current : null;
  const runningId = shown.sending ? shown.ownId : shown.serverTurn?.at(-1) ?? null;
  const interruptible = !runningId || !shown.interrupt?.ids.includes(runningId) || shown.interrupt.can;
  return {
    identity: { kind: "manager", sessionId: sid }, messages: shown.owned ? shown.messages : [],
    canSend: shown.owned, history: shown.history, historyError: shown.error,
    loadingText: "正在确认会话归属…", onRetry: () => setRetry(value => value + 1),
    refusal: replaced === sid ? FOREIGN : undefined,
    draft: shown.owned ? shown.draft : "", staged: shown.owned ? shown.staged : [],
    inputHistory: shown.owned ? conversation.value.history : [],
    phase: shown.phase, replying: shown.sending || shown.serverTurn !== null,
    canStop: stopEnabled && runningId !== null && interruptible,
    stopRefusal: stopEnabled && runningId !== null && !interruptible ? CANNOT_INTERRUPT : undefined,
    notice: shown.notice,
    onDraft: draft => { if (owner && active.current === owner) { owner.thread.draft = draft; store.patch(cacheKey, { draft }); owner.publish(); } },
    onStaged: staged => { if (owner && active.current === owner) { owner.thread.staged = staged; store.patch(cacheKey, { staged: [...staged] }); owner.publish(); } },
    onSend: text => {
      if (!owner?.thread.owned || active.current !== owner || sidRef.current !== sid) return;
      owner.thread.draft = ""; store.patch(cacheKey, { draft: "" }); owner.send(text);
    },
    onStop: () => owner?.stop(),
  };
}
