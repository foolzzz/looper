import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  AlertCircle,
  Bell,
  Circle,
  CircleDashed,
  CircleDot,
  CircleSlash,
  CornerDownRight,
  Eye,
  FileCode,
  GitCommitHorizontal,
  Hourglass,
  Info,
  Loader2,
  type LucideIcon,
  Megaphone,
  MessageCircleQuestionMark,
  Plug,
  TriangleAlert,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  fetchTaskRoom,
  isActiveTask,
  type RoomMessage,
  type RoomMessageKind,
  type RoomPresence,
  type RoomSeat,
  type RoomView,
  type TaskDetail,
} from "@/lib/api";
import {
  PRESENCE_LABELS,
  ROOM_KIND_LABELS,
  type RoomFilter,
  audienceText,
  filterMessages,
  fromLooper,
  isUnanswered,
  mergeRoom,
  reasonText,
  roomAnchor,
  roomRoles,
} from "@/lib/room";
import { cn } from "@/lib/utils";

/** How often a running task's room is read again. */
export const ROOM_REFRESH_MS = 3000;

type BadgeVariant = "default" | "warning" | "outline";

const KINDS: Record<RoomMessageKind, { icon: LucideIcon; variant: BadgeVariant }> = {
  info: { icon: Info, variant: "outline" },
  question: { icon: MessageCircleQuestionMark, variant: "outline" },
  answer: { icon: CornerDownRight, variant: "outline" },
  interface: { icon: Plug, variant: "default" },
  announce: { icon: Megaphone, variant: "outline" },
  notice: { icon: Bell, variant: "outline" },
  escalation: { icon: TriangleAlert, variant: "warning" },
};

const PRESENCES: Record<RoomPresence, { icon: LucideIcon; tone: string }> = {
  online: { icon: CircleDot, tone: "text-success" },
  waiting: { icon: Hourglass, tone: "text-warning" },
  offline: { icon: Circle, tone: "text-muted-foreground" },
  planned: { icon: CircleDashed, tone: "text-muted-foreground" },
  dropped: { icon: CircleSlash, tone: "text-muted-foreground" },
  consulting: { icon: Loader2, tone: "text-primary" },
};

const FILTERS: { id: RoomFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "interface", label: "Interface" },
  { id: "unanswered", label: "Unanswered" },
];

function clock(iso: string) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString([], { hour12: false });
}

function when(iso: string) {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** The task's room, read once and, while the task is active, every `ROOM_REFRESH_MS` for the newer messages. A failed
 * read keeps what was read before. */
function useRoom(taskId: string, active: boolean) {
  const [view, setView] = useState<RoomView | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const last = useRef(0);

  useEffect(() => {
    // Per run of the effect: a read of an earlier run (before the task ended) never holds back this one's.
    let stopped = false;
    let busy = false;
    const load = async () => {
      if (busy) return;
      busy = true;
      try {
        const after = last.current;
        let next = await fetchTaskRoom(taskId, { after });
        // More newer messages than one read gives: read the newest whole, so none in between is left out.
        const whole = !after || next.more;
        if (after && next.more) next = await fetchTaskRoom(taskId);
        if (stopped) return;
        const newest = next.messages.length ? next.messages[next.messages.length - 1].id : 0;
        last.current = whole ? newest : Math.max(after, newest);
        setView((prev) => mergeRoom(prev, next, !whole));
        setError("");
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : String(err));
      } finally {
        busy = false;
      }
    };
    // Also once when the task ends, for its last messages.
    load();
    if (!active) return () => { stopped = true; };
    const timer = setInterval(load, ROOM_REFRESH_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [taskId, active, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { view, error, retry };
}

function PresenceLine({ presence, since }: { presence: RoomPresence; since?: string }) {
  const { icon: Icon, tone } = PRESENCES[presence] ?? PRESENCES.offline;
  return (
    <span className="flex items-center gap-1 text-muted-foreground">
      <Icon className={cn("h-3 w-3 shrink-0", tone)} aria-hidden="true" />
      <span>
        {PRESENCE_LABELS[presence] ?? presence}
        {since && (presence === "online" || presence === "waiting") && (
          <span title={when(since)}> since {clock(since)}</span>
        )}
      </span>
    </span>
  );
}

/** Who is in the room: the Planner first (there while its task is active), then each step's seats. */
function Roster({ seats, plannerOnline }: { seats: RoomSeat[]; plannerOnline: boolean }) {
  return (
    <section aria-labelledby="room-seats-title">
      <h3 id="room-seats-title" className="mb-2 text-sm font-semibold">
        Seats
      </h3>
      <ul className="flex flex-wrap gap-2">
        <li className="min-w-0 rounded-md border border-border bg-card px-3 py-2 text-xs">
          <p className="font-mono font-medium">planner</p>
          <PresenceLine presence={plannerOnline ? "online" : "offline"} />
        </li>
        {seats.map((seat) => (
          <li key={seat.seat} className="min-w-0 rounded-md border border-border bg-card px-3 py-2 text-xs">
            {/* The seat's name, <role>@<step key>, with its step set off. */}
            <p className="break-all font-mono">
              <span className="font-medium">{seat.role}</span>
              <span className="text-muted-foreground">@{seat.step}</span>
              {seat.kind === "review" && <span className="font-sans text-muted-foreground"> · review</span>}
            </p>
            <PresenceLine presence={seat.presence} since={seat.since} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function KindBadge({ kind }: { kind: RoomMessageKind }) {
  const { icon: Icon, variant } = KINDS[kind] ?? KINDS.info;
  return (
    <Badge variant={variant} className="gap-1">
      <Icon className="h-3 w-3" aria-hidden="true" />
      {ROOM_KIND_LABELS[kind] ?? kind}
    </Badge>
  );
}

function Refs({ message }: { message: RoomMessage }) {
  const refs = [
    ...(message.files ?? []).map((file) => ({ key: `f:${file}`, text: file, Icon: FileCode, label: "File" })),
    ...(message.commits ?? []).map((commit) => ({ key: `c:${commit}`, text: commit, Icon: GitCommitHorizontal, label: "Commit" })),
  ];
  if (!refs.length) return null;
  return (
    <ul className="mt-2 flex flex-wrap gap-1">
      {refs.map(({ key, text, Icon, label }) => (
        <li
          key={key}
          title={`${label}: ${text}`}
          className="inline-flex min-w-0 items-center gap-1 break-all rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground"
        >
          <Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
          <span className="sr-only">{label}: </span>
          {text}
        </li>
      ))}
    </ul>
  );
}

/** One message: its number, kind, sender and audience, what it answers, its marks, its text and its references;
 * Looper's own muted, with why it wrote it. */
export function RoomMessageItem({ message, known, onShow }: {
  message: RoomMessage;
  /** Whether the message it answers is loaded, so "re #N" can take the user there. */
  known: boolean;
  onShow: (id: number) => void;
}) {
  const looper = fromLooper(message);
  return (
    <li
      id={roomAnchor(message.id)}
      tabIndex={-1}
      className={cn(
        "rounded-lg border px-4 py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        looper ? "border-dashed border-border bg-muted/40 text-muted-foreground" : "border-border bg-card",
      )}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span className="font-mono text-muted-foreground">#{message.id}</span>
        <KindBadge kind={message.kind} />
        <span className="min-w-0 break-all font-mono">
          {message.from} <span className="text-muted-foreground" aria-hidden="true">→</span>
          <span className="sr-only">to</span> {audienceText(message.to)}
        </span>
        {message.reply_to != null &&
          (known ? (
            <button
              type="button"
              onClick={() => onShow(message.reply_to!)}
              className="rounded font-mono text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              re #{message.reply_to}
            </button>
          ) : (
            <span className="font-mono text-muted-foreground">re #{message.reply_to}</span>
          ))}
        {message.via === "planner_reply" && <Badge variant="outline">Planner&apos;s reply</Badge>}
        {message.via === "consult" && <Badge variant="outline">Via consult</Badge>}
        {message.cc_planner && (
          <Badge variant="outline" className="gap-1">
            <Eye className="h-3 w-3" aria-hidden="true" />
            Seen by the Planner
          </Badge>
        )}
        {isUnanswered(message) && <Badge variant="warning">Unanswered</Badge>}
        <time dateTime={message.at} title={when(message.at)} className="ml-auto font-mono tabular-nums text-muted-foreground">
          {clock(message.at)}
        </time>
      </div>
      {looper && message.reason && <p className="mt-1.5 text-xs">{reasonText(message.reason)}</p>}
      <p className={cn("mt-1.5 whitespace-pre-wrap break-words text-sm", !looper && "text-foreground")}>{message.body}</p>
      <Refs message={message} />
    </li>
  );
}

/** The task's agent room, read only: who is in it, then its messages oldest first, filtered by kind or role. */
export function TaskRoomTab({ task }: { task: TaskDetail }) {
  const active = isActiveTask(task);
  const { view, error, retry } = useRoom(task.task_id, active);
  const [filter, setFilter] = useState<RoomFilter>("all");
  const [role, setRole] = useState("");
  // "re #N": to that message, after clearing a filter that hides it.
  const show = useCallback((id: number) => {
    if (!document.getElementById(roomAnchor(id))) {
      flushSync(() => {
        setFilter("all");
        setRole("");
      });
    }
    const place = document.getElementById(roomAnchor(id));
    place?.scrollIntoView({ block: "center" });
    place?.focus({ preventScroll: true });
  }, []);

  if (!view) {
    return (
      <div className="p-5">
        {error ? (
          <div className="flex flex-wrap items-center gap-3">
            <p role="alert" className="flex items-center gap-1.5 text-sm text-destructive">
              <AlertCircle className="h-4 w-4" aria-hidden="true" />
              Could not load the room: {error}
            </p>
            <Button type="button" variant="outline" size="sm" onClick={retry}>
              Try again
            </Button>
          </div>
        ) : (
          <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            Loading the room…
          </p>
        )}
      </div>
    );
  }

  const { counts } = view;
  const ids = new Set(view.messages.map((m) => m.id));
  const shown = filterMessages(view.messages, filter, role);
  const filterCounts: Record<RoomFilter, number> = {
    all: counts.messages,
    interface: counts.by_kind.interface ?? 0,
    unanswered: counts.unanswered,
  };
  const failed = error && (
    <p role="status" className="flex items-center gap-1.5 text-xs text-destructive">
      <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      Refresh failed ({error}); showing what was read before.
    </p>
  );

  if (!view.enabled && counts.messages === 0) {
    return (
      <div className="space-y-3 p-5">
        {failed}
        <div className="rounded-lg border border-border bg-card px-4 py-6 text-center text-sm">
          <p className="text-muted-foreground">This project has the agent room turned off.</p>
          {task.project_id && (
            <a
              href={`#projects/${encodeURIComponent(task.project_id)}/settings`}
              className="mt-2 inline-block text-xs font-medium text-primary underline-offset-2 hover:underline"
            >
              Open the project&apos;s settings
            </a>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5 p-5">
      {failed}
      {!view.enabled && (
        <p className="rounded-lg border border-border bg-card px-4 py-3 text-sm text-muted-foreground">
          This project has the agent room turned off. The messages below were posted while it was on; they are read only.
        </p>
      )}
      <Roster seats={view.seats} plannerOnline={active} />

      <section aria-labelledby="room-messages-title" className="space-y-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <h3 id="room-messages-title" className="text-sm font-semibold">
            Messages
          </h3>
          <div role="group" aria-label="Show" className="flex flex-wrap items-center gap-1">
            {FILTERS.map(({ id, label }) => (
              <Button
                key={id}
                type="button"
                variant="outline"
                size="sm"
                aria-pressed={filter === id}
                onClick={() => setFilter(id)}
                className={cn(filter === id && "border-primary/40 bg-primary/10 text-primary hover:bg-primary/15 hover:text-primary")}
              >
                {label}
                <span className="font-mono tabular-nums opacity-70">{filterCounts[id]}</span>
              </Button>
            ))}
          </div>
          <select
            aria-label="From or to a role"
            value={role}
            onChange={(e) => setRole(e.target.value)}
            className="h-8 min-w-0 max-w-full rounded-md border border-input bg-transparent px-2 text-xs text-foreground"
          >
            <option value="">From or to any role</option>
            {roomRoles(view.seats).map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          {active && <span className="text-xs text-muted-foreground sm:ml-auto">Updates every {ROOM_REFRESH_MS / 1000} s</span>}
        </div>

        {view.more && (
          <p className="text-xs text-muted-foreground">
            Showing the newest {view.messages.length} messages; older ones are not loaded.
          </p>
        )}

        {counts.messages === 0 && view.messages.length === 0 ? (
          <p className="rounded-lg border border-border bg-card px-4 py-6 text-center text-sm text-muted-foreground">
            No messages in this task&apos;s room yet.
          </p>
        ) : shown.length === 0 ? (
          <div className="flex flex-wrap items-center justify-center gap-3 rounded-lg border border-border bg-card px-4 py-6 text-sm text-muted-foreground">
            No messages match this filter.
            <Button type="button" variant="outline" size="sm" onClick={() => { setFilter("all"); setRole(""); }}>
              Show all
            </Button>
          </div>
        ) : (
          <ol className="space-y-2">
            {shown.map((message) => (
              <RoomMessageItem
                key={message.id}
                message={message}
                known={message.reply_to != null && ids.has(message.reply_to)}
                onShow={show}
              />
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
