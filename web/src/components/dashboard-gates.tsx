import { useEffect, useRef, useState } from "react";
import { Loader2, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  fetchTasks, fetchTaskDetail, postGateMessage, GATE_MESSAGE_MAX_CHARS, isActiveTask,
  type TaskDetail, type UserGate,
} from "@/lib/api";
import { gateAnchor, gateWaitsOn, isGateWaiting, sinceText } from "@/lib/gates";
import { errorText, useAction } from "@/lib/use-action";

const LIMIT = 10;
const linkClass = "rounded-sm text-primary hover:underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
type Waiting = { task: TaskDetail; gate: UserGate };

function GateReply({ task, gate, onChanged }: Waiting & { onChanged: (gate: UserGate) => void }) {
  const [text, setText] = useState("");
  const { busy, error, run } = useAction();
  const input = useRef<HTMLTextAreaElement>(null);
  const href = `#tasks/${encodeURIComponent(task.task_id)}`;
  const id = `${task.task_id}-${gateAnchor(gate.step)}`;
  const question = gate.agenda.find((item) => item.status === "open");
  return (
    <article aria-labelledby={`${id}-topic`} className="min-w-0">
      <Card className="space-y-2 p-4 [overflow-wrap:anywhere]">
        <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-xs">
          <a href={href} className={linkClass}>{task.name}</a>
          {gate.opened_at && <time dateTime={gate.opened_at} title={new Date(gate.opened_at).toLocaleString()}
            className="text-muted-foreground">Waiting {sinceText(gate.opened_at)}</time>}
        </div>
        <h3 id={`${id}-topic`} className="text-sm font-semibold">{gate.topic}</h3>
        {question && <p className="whitespace-pre-wrap text-sm">{question.question}</p>}
        <form className="space-y-2" onSubmit={async (event) => {
          event.preventDefault();
          if (busy || !text.trim() || text.length > GATE_MESSAGE_MAX_CHARS) return;
          await run(async () => {
            const posted = await postGateMessage(task.task_id, gate.step, { text });
            setText("");
            onChanged(posted.gate);
          });
          input.current?.focus();
        }}>
          <label htmlFor={`${id}-reply`} className="sr-only">Your answer to {gate.topic}</label>
          <textarea ref={input} id={`${id}-reply`} value={text} disabled={busy}
            onChange={(event) => setText(event.target.value)} rows={2} maxLength={GATE_MESSAGE_MAX_CHARS}
            placeholder="Answer, ask back, or add what the Planner should know"
            className="block w-full min-w-0 rounded-lg border border-border bg-background px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50" />
          {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Button type="submit" disabled={busy || !text.trim() || text.length > GATE_MESSAGE_MAX_CHARS} className="min-h-11 focus-visible:ring-2">
              {busy ? <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                : <Send className="h-4 w-4" aria-hidden="true" />} Send
            </Button>
            <a href={`${href}?gate=${encodeURIComponent(gate.step)}`} className={`inline-flex min-h-11 items-center text-xs ${linkClass}`}>
              Open the discussion
            </a>
          </div>
        </form>
      </Card>
    </article>
  );
}

/** A bounded read of needs-you tasks; the dashboard's overview poll triggers another read. */
export function DashboardGates({ needsYou, revision }: { needsYou: number; revision: object }) {
  const [tasks, setTasks] = useState<TaskDetail[]>([]);
  const [moreTasks, setMoreTasks] = useState(0);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const latest = useRef(0);
  useEffect(() => {
    const request = ++latest.current;
    if (!needsYou) {
      setTasks([]);
      setMoreTasks(0);
      setError("");
      return;
    }
    void (async () => {
      try {
        const page = await fetchTasks({ group: "needs_you", limit: LIMIT });
        const found = await Promise.all(page.tasks.slice(0, LIMIT).map((task) => fetchTaskDetail(task.task_id)));
        if (request !== latest.current) return;
        setTasks(found);
        // Each task beyond this bounded read has at least one waiting gate; more may be found on Tasks.
        setMoreTasks(Math.max(0, Object.values(page.counts).reduce((sum, n) => sum + n, 0) - found.length));
        setError("");
      } catch (err) {
        if (request === latest.current) setError(errorText(err));
      }
    })();
    return () => { latest.current++; };
  }, [needsYou, revision, refresh]);

  const waiting = tasks.flatMap((task) => task.group === "completed" ? [] : task.gates
    .filter((gate) => isGateWaiting(gate) && gateWaitsOn(gate, isActiveTask(task)) === "user")
    .map((gate) => ({ task, gate })))
    .sort((a, b) => (Date.parse(b.gate.opened_at ?? "") || 0) - (Date.parse(a.gate.opened_at ?? "") || 0));
  const more = Math.max(0, waiting.length - LIMIT) + moreTasks;
  const failure = error && <p role="alert" className="text-xs text-destructive [overflow-wrap:anywhere]">
    Cannot refresh waiting gates ({error}). <a href="#tasks?group=needs_you" className={linkClass}>View tasks needing you</a>
  </p>;
  if (!needsYou || !waiting.length) return failure || null;
  return (
    <section aria-label="Waiting for you" className="space-y-3">
      <h2 className="text-sm font-semibold">Waiting for you</h2>
      {failure}
      <div className="grid min-w-0 gap-3 lg:grid-cols-2">
        {waiting.slice(0, LIMIT).map(({ task, gate }) => (
          <GateReply key={`${task.task_id}/${gate.step}`} task={task} gate={gate} onChanged={(updated) => {
            // A poll started before the send must not restore the previous turn.
            latest.current++;
            setTasks((current) => current.map((t) => t.task_id !== task.task_id ? t : {
              ...t, gates: t.gates.map((g) => g.step_id === updated.step_id ? updated : g),
            }));
            setRefresh((n) => n + 1);
          }} />
        ))}
      </div>
      {more > 0 && <a href="#tasks?group=needs_you" className={`inline-flex min-h-11 items-center text-sm ${linkClass}`}>
        and {moreTasks > 0 ? "at least " : ""}{more} more
      </a>}
    </section>
  );
}
