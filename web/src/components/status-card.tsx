import { useEffect, useState } from "react";
import { startPolling } from "@/lib/polling";
import { fetchStatusCard, isStatusCardOff, type StatusManager, type StatusSection, type StatusSession,
  type StatusSnapshot } from "@/lib/status-card";
import { cn } from "@/lib/utils";

const REFRESH_MS = 15_000;
const linkClass = "underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded-sm";
const WAITING = { gates: "gates", confirmations: "confirmations", schedules: "failed schedules", logins: "logins",
  intents: "intents", ledger: "to-dos", deliveries: "possibly undelivered",
  reminders: "reminders not confirmed delivered" } as Record<string, string>;
const BACKGROUND = { completion: "wrapping up a task", own: "its own turn" } as Record<string, string>;

const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
/** A due time in the zone the snapshot names (what `/status` shows), the browser's when it names none or an unknown one. */
const when = (iso: string, timeZone?: string | null) => {
  const d = new Date(iso);
  const fmt = (o: Intl.DateTimeFormatOptions) => {
    try { return d.toLocaleString([], { ...o, timeZone: timeZone || undefined }); }
    catch { return d.toLocaleString([], o); }
  };
  const day = fmt({ year: "numeric", month: "numeric", day: "numeric" });
  const today = new Date().toLocaleString([], { year: "numeric", month: "numeric", day: "numeric", timeZone: timeZone || undefined });
  const clock = fmt({ hour: "2-digit", minute: "2-digit" });
  return day === today ? clock : `${fmt({ month: "short", day: "numeric" })} ${clock}`;
};
function elapsed(seconds?: number | null): string {
  if (seconds == null || seconds < 60) return "under a minute";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const rest = minutes % 60;
  return `${Math.floor(minutes / 60)} h${rest ? ` ${rest} min` : ""}`;
}
function where(s: StatusSession): string {
  if (s.channel === "web") return s.client_id ? "an API client" : "the console";
  if (s.channel === "telegram") return s.group ? "a Telegram group" : "Telegram";
  if (s.channel === "lark" || s.channel === "lark_bot") return s.group ? "a Lark group" : "Lark";
  return "a chat";
}
function sessionText(s: StatusSession): string {
  if (s.state === "background") return `running a background turn (${BACKGROUND[s.origin ?? ""] ?? "its own turn"}, ${elapsed(s.elapsed_seconds)})`;
  return `answering you in ${where(s)} (${elapsed(s.elapsed_seconds)})`;
}

function Row({ label, section, children }: { label: string; section: StatusSection; children: React.ReactNode }) {
  const failed = section.state === "error";
  return (
    <div role={failed ? "alert" : undefined} className="flex flex-col gap-x-4 gap-y-0.5 py-2 text-sm sm:flex-row">
      <span className="w-28 shrink-0 text-xs text-muted-foreground sm:pt-0.5">{label}</span>
      <div className="min-w-0 flex-1">
        {failed ? <span className="text-destructive">{section.error || "Cannot read this part"}</span>
          : section.state === "unknown" ? <span className="text-muted-foreground">Not available here</span> : children}
      </div>
    </div>
  );
}

function ManagerLines({ managers }: { managers: StatusManager[] }) {
  return (
    <ul className="space-y-1">
      {managers.map((m) => {
        const busy = m.sessions.filter((s) => s.state !== "idle");
        return (
          <li key={m.manager_id} className="break-words">
            {managers.length > 1 && <span className="font-medium">{m.name || m.manager_id}: </span>}
            {busy.length ? busy.map(sessionText).join("; ") : "Idle"}
            <span className="text-muted-foreground"> · {m.queued} queued</span>
          </li>
        );
      })}
    </ul>
  );
}

function Rows({ sections }: { sections: StatusSection[] }) {
  const get = (name: string) => sections.find((s) => s.name === name);
  const manager = get("manager") as Extract<StatusSection, { name: "manager" }> | undefined;
  const waiting = get("waiting") as Extract<StatusSection, { name: "waiting" }> | undefined;
  const reminders = get("reminders") as Extract<StatusSection, { name: "reminders" }> | undefined;
  const tasks = get("tasks") as Extract<StatusSection, { name: "tasks" }> | undefined;
  const schedules = get("schedules") as Extract<StatusSection, { name: "schedules" }> | undefined;
  const kinds = (waiting?.kinds ?? []).filter((k) => k.count > 0 || k.state !== "ok");
  const nextFire = schedules?.next?.find((n) => n.next_fire_at);
  return (
    <div className="divide-y divide-border">
      {manager && <Row label="Manager" section={manager}><ManagerLines managers={manager.managers ?? []} /></Row>}
      {waiting && (
        <Row label="Waiting for you" section={waiting}>
          {kinds.length === 0 ? <span className="text-muted-foreground">Nothing</span> : (
            <ul className="flex flex-wrap gap-x-4 gap-y-1">
              {kinds.map((k) => (
                <li key={k.kind} role={k.state === "error" ? "alert" : undefined}>
                  {k.state === "error"
                    ? <span className="text-destructive">{WAITING[k.kind] ?? k.kind}: {k.error || "cannot read"}</span>
                    : k.state === "unknown"
                      ? <span className="text-muted-foreground">{WAITING[k.kind] ?? k.kind}: Not available here</span>
                      : <a href="#todo" className={cn("text-warning", linkClass)}>{k.count} {WAITING[k.kind] ?? k.kind}</a>}
                </li>
              ))}
            </ul>
          )}
        </Row>
      )}
      {reminders && (
        <Row label="Next reminder" section={reminders}>
          {reminders.next ? (
            <span className="break-words">{reminders.next.due_at && <span className="text-muted-foreground">{when(reminders.next.due_at, reminders.next.timezone)} · </span>}{reminders.next.text}</span>
          ) : <span className="text-muted-foreground">None</span>}
        </Row>
      )}
      {tasks && <Row label="Tasks" section={tasks}><a href="#tasks" className={linkClass}>{tasks.count ?? 0} running</a></Row>}
      {schedules && (
        <Row label="Schedules" section={schedules}>
          <a href="#schedules" className={linkClass}>{schedules.running?.length ?? 0} running</a>
          <span className="text-muted-foreground">{nextFire ? ` · next ${when(nextFire.next_fire_at!, nextFire.timezone)}` : ""}</span>
        </Row>
      )}
    </div>
  );
}

/** What the Manager and the queues are doing now (`status_card.get`, the snapshot `/status` sends). Off (HTTP 409):
 * renders nothing and stops asking. A failed read keeps the last snapshot and says so. */
export function StatusCard() {
  const [snapshot, setSnapshot] = useState<StatusSnapshot | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    return startPolling(async (signal) => {
      try {
        const next = await fetchStatusCard(signal);
        if (signal.aborted) return;
        setSnapshot(next);
        setError("");
      } catch (e) {
        if (signal.aborted) return;
        if (isStatusCardOff(e)) { setSnapshot(null); setError(""); return true; }
        setError(e instanceof Error ? e.message : String(e));
      }
    }, REFRESH_MS);
  }, []);

  if (!snapshot && !error) return null;
  return (
    <section aria-label="Status" className="rounded-lg border border-border bg-card px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 className="text-sm font-semibold">Status</h2>
        {snapshot && <span className="text-xs text-muted-foreground">As of {time(snapshot.read_at)}</span>}
      </div>
      {error && (
        <p role="alert" className={cn("mt-2 text-xs text-destructive", snapshot && "mb-1")}>
          Cannot load the status ({error}){snapshot ? "; this is the last snapshot read." : "."}
        </p>
      )}
      {snapshot && <div className="mt-2"><Rows sections={snapshot.sections} /></div>}
    </section>
  );
}
