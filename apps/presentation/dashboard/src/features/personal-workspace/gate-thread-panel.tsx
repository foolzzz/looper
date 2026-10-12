import { useCallback, useEffect, useRef, useState } from "react";
import { Send } from "lucide-react";
import { ChatApiError, fetchGateThread, replyToGateThread } from "../../data/chat";
import type { GateThreadView } from "../../data/gate-thread";
import type { GateThreadState } from "./gate-decisions";
import { useWorkspaceI18n } from "./i18n";
import { useVisiblePolling } from "./use-visible-polling";

const GATE_REPLY_LIMIT = 4000;
// A reply from the CLI or the orchestrator shows up without reopening the drawer.
const GATE_THREAD_POLL_MS = 5_000;
const HIDDEN_ERROR_CODES = new Set(["not_a_user_gate", "gate_not_found", "goal_not_registered", "invalid_todo_id"]);

/** Discussion thread of a user gate: read the messages and append an owner reply.
 * Replying never closes the gate; approve/reject/cancel stay on gate.resolve.
 * While the panel is open and the page visible it re-reads the thread every few
 * seconds and on window focus, until the gate closes. `onState` receives each
 * successful read of this exact gate (`ready` or `closed`), or why there is
 * none (`unavailable`, `not_gate`); the drawer offers decisions only on `ready`. */
export function GateThreadPanel({ goalId, todoId, readOnly, onState }: {
  goalId: string; todoId: string; readOnly: boolean; onState?: (state: GateThreadState) => void;
}) {
  const { t } = useWorkspaceI18n();
  const [view, setView] = useState<GateThreadView | null>(null);
  const [hidden, setHidden] = useState(false);
  const [loadError, setLoadError] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const generation = useRef(0);
  const issued = useRef(0);
  const applied = useRef(0);
  const pending = useRef(0);
  const shown = useRef(false);
  // The last successful read of this gate, shown (but not decidable) while a later read fails.
  const lastView = useRef<GateThreadView | null>(null);
  const messagesRef = useRef<HTMLOListElement>(null);

  // `poll` skips while a read is in flight; `force` (open, after a reply) always reads.
  // Responses, failed ones included, apply in issue order, so a slow older read never
  // replaces a newer outcome.
  const load = useCallback(async (mode: "force" | "poll") => {
    // After a successful read, any failed read makes the gate non-actionable until a read succeeds.
    const refreshFailed = () => {
      const last = lastView.current;
      if (last && last.awaiting !== "closed") onState?.({ goalId, todoId, status: "retrying", view: last });
    };
    if (mode === "poll" && pending.current > 0) return;
    const mine = generation.current;
    const request = ++issued.current;
    pending.current += 1;
    try {
      const next = await fetchGateThread(goalId, todoId);
      if (mine !== generation.current || request < applied.current) return;
      applied.current = request;
      if (next.goal_id !== goalId || next.todo_id !== todoId) {
        // A read of another gate is never this gate's state.
        if (shown.current) {
          refreshFailed();
        } else {
          setLoadError(true);
          onState?.({ goalId, todoId, status: "unavailable" });
        }
        return;
      }
      shown.current = true;
      lastView.current = next;
      setView(next);
      setLoadError(false);
      onState?.({ goalId, todoId, status: next.awaiting === "closed" ? "closed" : "ready", view: next });
    } catch (error) {
      if (mine !== generation.current || request < applied.current) return;
      applied.current = request;
      if (shown.current) {
        // The last thread stays on screen; the next poll retries.
        refreshFailed();
        return;
      }
      if (mode === "poll") return; // the first read already reported this gate unavailable
      const code = error instanceof ChatApiError ? String(error.payload.error_code ?? "") : "";
      if (HIDDEN_ERROR_CODES.has(code)) {
        setHidden(true);
        onState?.({ goalId, todoId, status: "not_gate" });
        return;
      }
      setLoadError(true);
      if (!shown.current) onState?.({ goalId, todoId, status: "unavailable" });
    } finally {
      pending.current -= 1;
    }
  }, [goalId, todoId, onState]);

  useEffect(() => {
    generation.current += 1;
    shown.current = false;
    lastView.current = null;
    setView(null);
    setHidden(false);
    setLoadError(false);
    setDraft("");
    setSendError(null);
    void load("force");
    return () => { generation.current += 1; };
  }, [load]);

  const closed = view?.awaiting === "closed";
  useVisiblePolling(useCallback(() => void load("poll"), [load]), GATE_THREAD_POLL_MS, !hidden && !closed);

  const messageCount = view?.messages.length ?? 0;
  useEffect(() => {
    const list = messagesRef.current;
    if (list) list.scrollTop = list.scrollHeight;
  }, [messageCount]);

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await replyToGateThread(goalId, todoId, text);
      setDraft("");
      await load("force");
    } catch (error) {
      setSendError(error instanceof Error ? error.message : String(error));
    } finally {
      setSending(false);
    }
  }

  if (hidden) return null;
  return (
    <section className="personal-detail-card personal-gate-thread" data-testid="gate-thread" aria-label={t("gateThread.title")}>
      <header>
        <strong>{t("gateThread.title")}</strong>
        {view ? <small data-awaiting={view.awaiting}>{t(`gateThread.${view.awaiting}`)}</small> : null}
      </header>
      {loadError ? <p className="personal-gate-thread-empty">{t("gateThread.loadError")}</p> : null}
      {view && view.messages.length === 0 ? <p className="personal-gate-thread-empty">{t("gateThread.empty")}</p> : null}
      {view && view.messages.length ? (
        <ol aria-label={t("gateThread.messages")} aria-live="polite" aria-relevant="additions text" className="personal-gate-thread-messages" ref={messagesRef} role="log">
          {view.messages.map((message) => (
            <li className={`is-${message.author}`} key={message.message_id}>
              <small>{message.author === "user" ? t("gateThread.you") : t("gateThread.orchestrator", { agent: message.agent_id ?? "" })} · {message.at}</small>
              <p>{message.text}</p>
            </li>
          ))}
        </ol>
      ) : null}
      {!readOnly && view && !closed ? (
        <div className="personal-gate-thread-reply">
          <textarea
            aria-label={t("gateThread.reply")}
            maxLength={GATE_REPLY_LIMIT}
            onChange={(event) => setDraft(event.target.value.slice(0, GATE_REPLY_LIMIT))}
            placeholder={t("gateThread.replyPlaceholder")}
            rows={3}
            value={draft}
          />
          <button className="personal-secondary-action" disabled={!draft.trim() || sending} onClick={() => void send()} type="button">
            <Send size={15} />{sending ? t("gateThread.sending") : t("gateThread.send")}
          </button>
          {sendError ? <small className="personal-gate-thread-error">{sendError}</small> : null}
        </div>
      ) : null}
    </section>
  );
}
