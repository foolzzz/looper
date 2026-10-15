import { useCallback, useEffect, useState } from "react";
import { Bot, Loader2, MessageSquare, RefreshCw, User } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Markdown } from "@/components/markdown";
import {
  fetchChatMessages,
  fetchChatSessions,
  type ChatSession,
  type DbChatMessage,
} from "@/lib/api";
import { cn } from "@/lib/utils";

/** Each channel gets a labelled badge; the text always carries the channel, the colour is a hint. */
function channelBadge(channel: string) {
  switch (channel) {
    case "telegram":
      return <Badge variant="default">telegram</Badge>;
    case "lark":
      return <Badge variant="warning">lark</Badge>;
    case "web":
      return <Badge variant="outline">web</Badge>;
    default:
      return <Badge variant="outline">{channel || "—"}</Badge>;
  }
}

function formatTime(iso: string | null) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function SessionCard({
  session,
  selected,
  onSelect,
}: {
  session: ChatSession;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={selected ? "true" : undefined}
      className={cn(
        "w-full rounded-lg border p-3 text-left transition-colors",
        selected ? "border-primary bg-primary/5" : "border-border bg-card hover:bg-muted/50",
      )}
    >
      <div className="flex items-center gap-2">
        {channelBadge(session.channel)}
        <span className="ml-auto text-xs tabular-nums text-muted-foreground">
          {session.message_count} {session.message_count === 1 ? "message" : "messages"}
        </span>
      </div>
      <p className="mt-2 truncate font-mono text-xs text-foreground" title={session.session_id}>
        {session.session_id}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">{formatTime(session.last_message_at)}</p>
    </button>
  );
}

function MessageList({
  sessionId,
  messageCount,
  refreshKey,
}: {
  sessionId: string;
  messageCount: number;
  refreshKey: number;
}) {
  const [messages, setMessages] = useState<DbChatMessage[] | null>(null);

  useEffect(() => {
    let active = true;
    setMessages(null);
    fetchChatMessages(sessionId)
      .then((rows) => {
        if (active) setMessages(rows);
      })
      .catch(() => {
        if (active) setMessages([]);
      });
    return () => {
      active = false;
    };
    // refreshKey re-fetches the same conversation when the user clicks Refresh.
  }, [sessionId, refreshKey]);

  if (messages === null) {
    return (
      <div className="flex h-40 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (!messages.length) {
    return (
      <div className="flex h-40 items-center justify-center">
        <p className="text-sm text-muted-foreground">No messages in this conversation.</p>
      </div>
    );
  }
  const truncated = messageCount > messages.length;
  return (
    <div className="space-y-4">
      {truncated && (
        <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-center text-xs text-muted-foreground">
          Showing the oldest {messages.length} of {messageCount} messages. Paging to newer history is a follow-up.
        </p>
      )}
      {messages.map((msg) => {
        const isUser = msg.role === "user";
        return (
          <div key={msg.message_id} className={cn("flex gap-3", isUser ? "justify-end" : "justify-start")}>
            {!isUser && (
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/10">
                <Bot className="h-4 w-4 text-primary" />
              </div>
            )}
            <div className="min-w-0 max-w-[70%]">
              <div className="mb-1 flex items-center gap-2 text-[10px] text-muted-foreground">
                <span className="font-mono uppercase tracking-wider">{msg.role}</span>
                {msg.channel && channelBadge(msg.channel)}
                <span className="tabular-nums">{formatTime(msg.created_at)}</span>
              </div>
              <div
                className={cn(
                  "rounded-lg px-4 py-2.5 text-sm [overflow-wrap:anywhere]",
                  isUser ? "bg-primary text-primary-foreground" : "bg-muted text-foreground",
                )}
              >
                {isUser ? (
                  <p className="whitespace-pre-wrap">{msg.content}</p>
                ) : (
                  <Markdown>{msg.content}</Markdown>
                )}
              </div>
            </div>
            {isUser && (
              <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-accent">
                <User className="h-4 w-4 text-muted-foreground" />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** A read-only browser of every channel's chat sessions: pick one on the left, read its messages on the right. */
export function ConversationsPage() {
  const [sessions, setSessions] = useState<ChatSession[] | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  const load = useCallback(() => {
    fetchChatSessions()
      .then((rows) => {
        setSessions(rows);
        setError("");
        setSelected((prev) => prev ?? rows[0]?.session_id ?? null);
      })
      .catch((e) => {
        setSessions([]);
        setError(e instanceof Error ? e.message : String(e));
      });
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const current = sessions?.find((s) => s.session_id === selected) ?? null;

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Conversations</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Every channel&apos;s chat sessions, newest first, read-only.
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            load();
            refresh();
          }}
          className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <RefreshCw className="h-4 w-4" />
          Refresh
        </button>
      </div>

      {error && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          Failed to load conversations: {error}
        </div>
      )}

      {sessions === null ? (
        <div className="flex h-64 items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : sessions.length === 0 ? (
        <div className="flex h-64 flex-col items-center justify-center gap-3 text-center">
          <MessageSquare className="h-8 w-8 text-muted-foreground" />
          <p className="text-sm text-muted-foreground">
            No conversations yet. They appear here once a message arrives on any channel.
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-6 lg:flex-row">
          <div className="space-y-2 lg:w-80 lg:shrink-0">
            {sessions.map((s) => (
              <SessionCard
                key={s.session_id}
                session={s}
                selected={s.session_id === selected}
                onSelect={() => setSelected(s.session_id)}
              />
            ))}
          </div>

          {current && (
            <div className="min-w-0 flex-1 rounded-lg border border-border bg-card">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-4 py-3">
                {channelBadge(current.channel)}
                <span className="min-w-0 break-all font-mono text-xs text-muted-foreground">{current.session_id}</span>
                <span className="ml-auto text-xs tabular-nums text-muted-foreground">
                  {current.message_count} {current.message_count === 1 ? "message" : "messages"}
                </span>
              </div>
              <div className="p-5">
                <MessageList
                  sessionId={current.session_id}
                  messageCount={current.message_count}
                  refreshKey={refreshKey}
                />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
