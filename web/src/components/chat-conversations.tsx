import { Loader2, RefreshCw } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { ChatSession } from "@/lib/api";
import { CHAT_CHANNELS, channelLabel } from "@/lib/chat-channels";
import { cn } from "@/lib/utils";

/** A channel is a category, not a state: one neutral badge style for every channel, its name the content. */
export function ChannelBadge({ channel }: { channel: string }) {
  return <Badge variant="outline">{channelLabel(channel)}</Badge>;
}

function shortTime(iso: string): string {
  const at = new Date(iso);
  const today = new Date().toDateString() === at.toDateString();
  return today
    ? at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
    : at.toLocaleDateString([], { month: "short", day: "numeric" });
}

/**
 * The Manager's conversations on every channel, grouped by channel, each the most recently active first: who spoke
 * last and how it began, and when. ``selected`` is the open conversation's session id.
 */
export function ConversationList({
  sessions,
  error,
  selected,
  onSelect,
  onRefresh,
}: {
  sessions: ChatSession[] | null;
  error: string;
  selected: string;
  onSelect: (session: ChatSession) => void;
  onRefresh: () => void;
}) {
  // By channel in CHAT_CHANNELS' order; a session of another channel goes last.
  const known = new Set(CHAT_CHANNELS.map((c) => c.channel));
  const groups = [
    ...CHAT_CHANNELS.map(({ channel, label }) => ({ label, items: (sessions ?? []).filter((s) => s.channel === channel) })),
    { label: "Other", items: (sessions ?? []).filter((s) => !known.has(s.channel)) },
  ].filter((g) => g.items.length > 0);
  return (
    <nav aria-label="Conversations" className="flex h-full min-h-0 flex-col">
      <div className="flex items-center justify-between px-3 py-2">
        <span className="font-mono text-[10px] uppercase tracking-wider text-muted-foreground">Conversations</span>
        <button
          type="button"
          onClick={onRefresh}
          className="flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
          title="Reload the conversations"
          aria-label="Reload the conversations"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-2">
        {error && (
          <p role="alert" className="mx-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            Couldn&apos;t load the conversations: {error}
          </p>
        )}
        {sessions === null && !error && (
          <div className="flex justify-center py-6">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          </div>
        )}
        {sessions !== null && sessions.length === 0 && !error && (
          <p className="px-3 py-2 text-xs text-muted-foreground">
            No conversations yet. Each chat with the Manager, here or in Lark or Telegram, is listed once it has a message.
          </p>
        )}
        {groups.map((group) => (
          <section key={group.label} className="mt-1">
            <h3 className="px-3 pb-1 pt-2 text-xs font-medium text-muted-foreground">{group.label}</h3>
            <ul>
              {group.items.map((s) => (
                <li key={s.session_id}>
                  <button
                    type="button"
                    onClick={() => onSelect(s)}
                    aria-current={s.session_id === selected ? "true" : undefined}
                    title={s.session_id}
                    className={cn(
                      "w-full px-3 py-2 text-left transition-colors hover:bg-accent",
                      s.session_id === selected && "bg-accent/60",
                    )}
                  >
                    <div className="flex items-baseline gap-2">
                      <span className="min-w-0 flex-1 truncate text-sm">
                        <span className="text-muted-foreground">{s.last_role === "user" ? "User: " : "Manager: "}</span>
                        {s.last_content || "(empty)"}
                      </span>
                      <time dateTime={s.last_message_at} className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                        {shortTime(s.last_message_at)}
                      </time>
                    </div>
                    <p className="mt-0.5 text-[10px] text-muted-foreground">
                      {s.message_count} {s.message_count === 1 ? "message" : "messages"}
                    </p>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </nav>
  );
}
