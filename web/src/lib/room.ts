/** A task's agent room as the dashboard shows it: words for its kinds, presences and reasons, the timeline kept up to
 * date from `fetchTaskRoom`, its filters, a step's numbers, and the project's room limits. */
import type {
  ProjectExecution,
  RoomMessage,
  RoomMessageKind,
  RoomPresence,
  RoomSeat,
  RoomStepCounts,
  RoomView,
} from "@/lib/api";

export const ROOM_KIND_LABELS: Record<RoomMessageKind, string> = {
  info: "Info",
  question: "Question",
  answer: "Answer",
  interface: "Interface",
  announce: "Announcement",
  notice: "Notice",
  escalation: "Escalation",
};

export const PRESENCE_LABELS: Record<RoomPresence, string> = {
  online: "Online",
  waiting: "Waiting for room",
  offline: "Offline",
  planned: "Not started",
  dropped: "Dropped",
};

const REASONS: Record<string, string> = {
  ping_pong: "Two seats went back and forth too long without an answer.",
  consult_fallback: "The seat asked has no run to answer, so the question went to the Planner.",
  interface_changed: "An interface changed after a step that read the earlier one had ended.",
  room_full: "The task's room reached its message limit.",
  watch: "Something a run watched for happened.",
};

/** Why Looper wrote a notice or an escalation, in words; an unknown reason as it came. */
export const reasonText = (reason: string) => REASONS[reason] ?? reason;

/** Looper's own message: a notice or an escalation, not something an agent said. */
export const fromLooper = (message: RoomMessage) => message.from === "looper" || message.via === "looper";

export const isUnanswered = (message: RoomMessage) => message.kind === "question" && !message.answered;

/** A seat's role ("backend_rd@s1" → "backend_rd"); "planner", "looper" and a bare role stay as they are. */
export const roleOf = (seat: string) => seat.split("@", 1)[0];

/** Whom a message is for: "all", or its seats and roles. */
export const audienceText = (to: RoomMessage["to"]) => (to === "all" ? "all" : to.join(", "));

export const roomAnchor = (id: number) => `room-message-${id}`;

/** The room after a fetch: `next` itself when it is the whole room; with `after`, the earlier messages and the newer
 * ones once each, the questions those answer marked answered, and the roster and numbers from `next`. */
export function mergeRoom(prev: RoomView | null, next: RoomView, appended: boolean): RoomView {
  if (!appended || !prev) return next;
  const known = new Set(prev.messages.map((m) => m.id));
  const added = next.messages.filter((m) => !known.has(m.id));
  const answers = new Set(added.filter((m) => m.kind === "answer" && m.reply_to != null).map((m) => m.reply_to));
  const earlier = answers.size
    ? prev.messages.map((m) => (m.kind === "question" && answers.has(m.id) ? { ...m, answered: true } : m))
    : prev.messages;
  return { ...next, messages: [...earlier, ...added], more: prev.more };
}

export type RoomFilter = "all" | "interface" | "unanswered";

/** The messages a filter keeps; with a role, those from it or addressed to it (or to one of its seats). */
export function filterMessages(messages: RoomMessage[], filter: RoomFilter, role: string): RoomMessage[] {
  return messages.filter(
    (m) =>
      (filter === "all" || (filter === "interface" ? m.kind === "interface" : isUnanswered(m))) &&
      (!role || roleOf(m.from) === role || (m.to !== "all" && m.to.some((to) => roleOf(to) === role))),
  );
}

/** The roles to filter by: the Planner, then each role of the roster once, in its order. */
export const roomRoles = (seats: RoomSeat[]) => [...new Set(["planner", ...seats.map((s) => s.role)])];

/** A step's room numbers on its Pipeline node: "✉ 3", and "1 unanswered" when it has such questions. */
export function roomNote(counts: RoomStepCounts | undefined): string {
  if (!counts) return "";
  const note = `✉ ${counts.sent + counts.received}`;
  return counts.unanswered > 0 ? `${note} · ${counts.unanswered} unanswered` : note;
}

export type RoomLimitField = Extract<
  keyof ProjectExecution,
  | "room_post_per_minute"
  | "room_broadcasts_per_run"
  | "room_message_max_chars"
  | "room_messages_per_task"
  | "room_ping_pong_rounds"
  | "room_wait_max_seconds"
>;

/** The project's room limits, with their ranges and defaults as the server checks them. */
export const ROOM_LIMITS: { field: RoomLimitField; label: string; help: string; min: number; max: number; fallback: number }[] = [
  { field: "room_post_per_minute", label: "Posts per minute", help: "Messages one run may post in a minute.",
    min: 1, max: 60, fallback: 6 },
  { field: "room_broadcasts_per_run", label: "Broadcasts per run", help: "Messages to everyone one run may post.",
    min: 0, max: 50, fallback: 5 },
  { field: "room_message_max_chars", label: "Message length (characters)", help: "The most characters one message may have.",
    min: 500, max: 20000, fallback: 4000 },
  { field: "room_messages_per_task", label: "Messages per task", help: "The most messages a task's room may hold.",
    min: 20, max: 5000, fallback: 300 },
  { field: "room_ping_pong_rounds", label: "Back-and-forth rounds",
    help: "Messages two seats may trade before the Planner is told.", min: 2, max: 50, fallback: 6 },
  { field: "room_wait_max_seconds", label: "Inbox wait (s)", help: "How long one inbox call may wait for a message.",
    min: 0, max: 120, fallback: 60 },
];
