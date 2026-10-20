import { useEffect, useRef, useState } from "react";
import { Hourglass } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cancelWait, isActiveTask, resumeWait, type TaskDetail, type TaskWait } from "@/lib/api";
import { useAction } from "@/lib/use-action";
import { WAIT_STATES, waitAnchor, waitCheckText } from "@/lib/waits";

export function WaitBadge({ state }: { state: TaskWait["state"] }) {
  const look = WAIT_STATES[state];
  return <Badge variant={look.variant}>{look.label}</Badge>;
}

function WaitTime({ iso }: { iso: string | null }) {
  if (!iso) return <>—</>;
  const date = new Date(iso);
  const seconds = (date.getTime() - Date.now()) / 1000;
  const [unit, size] = Math.abs(seconds) >= 86400 ? ["day", 86400] as const
    : Math.abs(seconds) >= 3600 ? ["hour", 3600] as const
    : Math.abs(seconds) >= 60 ? ["minute", 60] as const : ["second", 1] as const;
  const relative = new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(Math.round(seconds / size), unit);
  return <time dateTime={iso}>{relative} · {date.toLocaleString()}</time>;
}

/** The same inline confirmation used for destructive actions elsewhere in the dashboard. */
function WaitActions({ taskId, wait, onChanged }: { taskId: string; wait: TaskWait; onChanged: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const ask = useRef<HTMLButtonElement>(null);
  const keep = useRef<HTMLButtonElement>(null);
  const asked = useRef(false);
  const { busy, error, run } = useAction();
  useEffect(() => {
    if (confirming) keep.current?.focus();
    else if (asked.current) ask.current?.focus();
    asked.current = confirming;
  }, [confirming]);

  const move = (cancel: boolean) => void run(async () => {
    await (cancel ? cancelWait : resumeWait)(taskId, wait.step);
    onChanged();
  });

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        {confirming ? (
          <>
            <span className="text-sm text-muted-foreground">Cancel this wait? The Planner decides what happens next.</span>
            <Button variant="outline" disabled={busy} onClick={() => move(true)}>Confirm cancel</Button>
            <Button ref={keep} variant="ghost" disabled={busy} onClick={() => setConfirming(false)}>Keep waiting</Button>
          </>
        ) : (
          <>
            <Button disabled={busy} onClick={() => move(false)}>Resume now</Button>
            <Button ref={ask} variant="outline" disabled={busy} onClick={() => setConfirming(true)}>Cancel</Button>
          </>
        )}
      </div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </div>
  );
}

export function WaitPanel({ taskId, active, wait, onChanged }: {
  taskId: string; active: boolean; wait: TaskWait; onChanged: () => void;
}) {
  const facts = [
    ["Opened", wait.opened_at], ["Earliest resume", wait.due_at], ["Deadline", wait.deadline_at],
    ["Next check", wait.next_check_at], ["Last checked", wait.last_checked_at], ["Closed", wait.closed_at],
  ] as const;
  return (
    <details id={waitAnchor(wait.step)} open={wait.state === "waiting" || wait.state === "timed_out" || wait.state === "cancel_requested"}
      className="rounded-lg border border-border bg-card">
      <summary className="flex min-h-11 cursor-pointer flex-wrap items-center gap-2 p-4 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        <Hourglass className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
        <span className="font-mono text-xs">wait {wait.step}</span>
        <WaitBadge state={wait.state} />
        <span className="min-w-0 break-words text-sm">{wait.topic}</span>
      </summary>
      <div className="space-y-3 border-t border-border p-4">
        <p className="break-words text-sm whitespace-pre-wrap">{waitCheckText(wait.check)}</p>
        {wait.every_seconds != null && <p className="text-xs text-muted-foreground">Every {wait.every_seconds / 60} min</p>}
        <dl className="space-y-1 text-xs">
          {facts.filter(([label, iso]) => iso || ["Opened", "Earliest resume", "Deadline", "Next check"].includes(label)).map(([label, iso]) => (
            <div key={label} className="grid gap-1 sm:grid-cols-2">
              <dt className="text-muted-foreground">{label}</dt><dd><WaitTime iso={iso} /></dd>
            </div>
          ))}
          <div className="grid gap-1 sm:grid-cols-2">
            <dt className="text-muted-foreground">Checks</dt><dd>{wait.checks}</dd>
          </div>
          <div className="grid gap-1 sm:grid-cols-2">
            <dt className="text-muted-foreground">Last result</dt><dd>{wait.last_result?.replaceAll("_", " ") ?? "Not checked yet"}</dd>
          </div>
          {wait.outcome && <div className="grid gap-1 sm:grid-cols-2"><dt className="text-muted-foreground">Outcome</dt><dd>{wait.outcome}</dd></div>}
          {(wait.state === "timed_out" || wait.notice) && (
            <div className="grid gap-1 sm:grid-cols-2">
              <dt className="text-muted-foreground">Timeout notice</dt>
              <dd>{wait.notice === "sent" ? "Notice sent" : wait.notice === "dashboard" ? "Dashboard" : wait.notice === "failed" ? "Failed" : "Not sent yet"}</dd>
            </div>
          )}
        </dl>
        {wait.last_error && <p className="break-words text-sm text-destructive">{wait.last_error}</p>}
        {wait.last_output && (
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">Last output</summary>
            <pre className="mt-2 max-h-60 overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap break-words">{wait.last_output}</pre>
          </details>
        )}
        {wait.resolution && <p className="break-words text-sm whitespace-pre-wrap">{wait.resolution}</p>}
        {active && wait.state === "waiting" && <WaitActions taskId={taskId} wait={wait} onChanged={onChanged} />}
        {(wait.state === "timed_out" || wait.state === "cancel_requested") && (
          <p className="text-sm text-muted-foreground">The Planner decides what happens next</p>
        )}
      </div>
    </details>
  );
}

export function TaskWaits({ task, onChanged }: { task: TaskDetail; onChanged: () => void }) {
  if (!task.waits?.length) return null;
  return <div className="space-y-3">{task.waits.map((wait) => <WaitPanel key={wait.step_id} taskId={task.task_id} active={isActiveTask(task)} wait={wait} onChanged={onChanged} />)}</div>;
}
