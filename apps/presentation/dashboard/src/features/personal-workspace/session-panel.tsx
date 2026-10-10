import { useCallback, useEffect, useState } from "react";
import { MessageSquarePlus, RotateCcw, X } from "lucide-react";

import {
  createChatSession,
  closeChatSession,
  fetchChatSessions,
  type ChatSessionSummary,
} from "../../data/chat";
import { useWorkspaceI18n } from "./i18n";

function formatTime(iso: string, locale: string): string {
  try {
    const date = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return locale === "zh-CN" ? "刚刚" : "just now";
    if (diffMin < 60) return locale === "zh-CN" ? `${diffMin} 分钟前` : `${diffMin}m ago`;
    const diffHour = Math.floor(diffMin / 60);
    if (diffHour < 24) return locale === "zh-CN" ? `${diffHour} 小时前` : `${diffHour}h ago`;
    const diffDay = Math.floor(diffHour / 24);
    if (diffDay < 7) return locale === "zh-CN" ? `${diffDay} 天前` : `${diffDay}d ago`;
    return date.toLocaleDateString(locale === "zh-CN" ? "zh-CN" : "en-US", { month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

export function SessionPanel({
  activeSessionId,
  agentId,
  channelId,
  goalId,
  onClose,
  onSwitchSession,
}: {
  activeSessionId: string | null;
  agentId: string;
  channelId: string;
  goalId: string | null;
  onClose: () => void;
  onSwitchSession: (sessionId: string, isNew: boolean) => void;
}) {
  const { locale, t } = useWorkspaceI18n();
  const [sessions, setSessions] = useState<ChatSessionSummary[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadSessions = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await fetchChatSessions({
        channelId,
        ...(goalId ? { goalId } : {}),
      });
      setSessions(result.sessions.filter((s) => s.status !== "closed"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to load sessions");
    } finally {
      setBusy(false);
    }
  }, [channelId, goalId]);

  useEffect(() => {
    void loadSessions();
  }, [loadSessions]);

  async function handleNewSession() {
    setBusy(true);
    setError(null);
    try {
      const result = await createChatSession(
        goalId ?? "",
        agentId,
        "new",
        goalId ? "goal" : "manager",
      );
      onSwitchSession(result.session_id, true);
      void loadSessions();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Failed to create session");
    } finally {
      setBusy(false);
    }
  }

  async function handleCloseSession(sessionId: string) {
    setBusy(true);
    try {
      await closeChatSession(sessionId);
      setSessions((prev) => prev.filter((s) => s.session_id !== sessionId));
    } catch {
      // ignore close failures
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="personal-session-panel">
      <div className="personal-session-panel-header">
        <strong>{t("session.list")}</strong>
        <div className="personal-session-panel-actions">
          <button
            type="button"
            onClick={() => void handleNewSession()}
            disabled={busy}
            title={t("session.new")}
          >
            <MessageSquarePlus size={14} />
            <span>{t("session.new")}</span>
          </button>
          <button
            type="button"
            onClick={() => void loadSessions()}
            disabled={busy}
            title={t("session.refresh")}
          >
            <RotateCcw size={14} />
          </button>
          <button type="button" onClick={onClose} className="personal-session-panel-close">
            <X size={14} />
          </button>
        </div>
      </div>

      {error ? <div className="personal-machine-error" role="alert">{error}</div> : null}

      <div className="personal-session-list">
        {sessions.length === 0 && !busy ? (
          <p className="personal-session-empty">{t("session.empty")}</p>
        ) : null}
        {sessions.map((session) => {
          const isActive = session.session_id === activeSessionId;
          return (
            <div
              key={session.session_id}
              className={`personal-session-row${isActive ? " is-active" : ""}`}
            >
              <button
                type="button"
                className="personal-session-select"
                onClick={() => {
                  if (!isActive) onSwitchSession(session.session_id, false);
                }}
                disabled={isActive || busy}
              >
                <span className="personal-session-time">
                  {formatTime(session.created_at, locale)}
                </span>
                <span className={`personal-session-status is-${session.status}`}>
                  {session.status === "ready" ? t("session.statusReady")
                    : session.status === "busy" ? t("session.statusBusy")
                    : session.status}
                </span>
                {isActive ? (
                  <span className="personal-session-current-badge">{t("session.current")}</span>
                ) : null}
              </button>
              {!isActive ? (
                <button
                  type="button"
                  className="personal-session-close-btn"
                  onClick={() => void handleCloseSession(session.session_id)}
                  disabled={busy}
                  title={t("session.close")}
                >
                  <X size={12} />
                </button>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
