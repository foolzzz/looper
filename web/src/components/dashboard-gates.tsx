import { useEffect, useRef, useState } from "react";
import { Loader2, Send } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  fetchTasks, fetchTaskDetail, postGateMessage, GATE_MESSAGE_MAX_CHARS, isActiveTask, TaskDetailError,
  type TaskDetail, type UserGate,
} from "@/lib/api";
import { gateAnchor, gateWaitsOn, isGateWaiting, sinceText } from "@/lib/gates";
import { errorText, useAction } from "@/lib/use-action";

const LIMIT = 10;
const linkClass = "rounded-sm text-primary hover:underline underline-offset-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
type Reply = { taskId: string; stepId: string; text: string };
const gateKey = (taskId: string, stepId: string) => `${taskId}/${stepId}`;

export function GateReply({ task, gate, reply, onChanged }: {
  task: Pick<TaskDetail, "task_id" | "name" | "group">; gate: UserGate;
  reply?: Reply; onChanged: (gate: UserGate, text: string) => void;
}) {
  const [text, setText] = useState("");
  const { busy, error, run } = useAction();
  const input = useRef<HTMLTextAreaElement>(null);
  const href = `#tasks/${encodeURIComponent(task.task_id)}`;
  const id = `${task.task_id}-${gateAnchor(gate.step)}`;
  const plannerAnswering = gateWaitsOn(gate, isActiveTask(task)) === "planner";
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
        {reply && <p role="status" className="whitespace-pre-wrap text-sm">You replied: {reply.text}</p>}
        {plannerAnswering && <p className="text-xs text-muted-foreground">Planner answering</p>}
        <form className="space-y-2" onSubmit={async (event) => {
          event.preventDefault();
          if (busy || !text.trim() || text.length > GATE_MESSAGE_MAX_CHARS) return;
          await run(async () => {
            const posted = await postGateMessage(task.task_id, gate.step, { text });
            setText("");
            onChanged(posted.gate, text);
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
  const [replies, setReplies] = useState<Reply[]>([]);
  const pendingReplies = useRef<Reply[]>([]);
  const loading = useRef(false);
  const queued = useRef<(() => Promise<void>) | null>(null);
  useEffect(() => () => {
    latest.current++;
    queued.current = null;
  }, []);
  useEffect(() => {
    // Polls replace the queued refresh, never the read already in flight.
    queued.current = async () => {
      const request = latest.current;
      try {
        const page = needsYou ? await fetchTasks({ group: "needs_you", limit: LIMIT })
          : { tasks: [], counts: {} };
        const pageIds = page.tasks.slice(0, LIMIT).map((task) => task.task_id);
        // A sent reply stays visible even when its task leaves the first page (or Needs you).
        const ids = [...new Set([...pageIds, ...pendingReplies.current.map((reply) => reply.taskId)])];
        const results = await Promise.allSettled(ids.map((id) => fetchTaskDetail(id)));
        if (request !== latest.current) return;
        const found: TaskDetail[] = [];
        const failedIds = new Set<string>();
        const errors: string[] = [];
        results.forEach((result, index) => {
          if (result.status === "fulfilled") found.push(result.value);
          else if (!(result.reason instanceof TaskDetailError && result.reason.status === 404)) {
            failedIds.add(ids[index]);
            errors.push(errorText(result.reason));
          }
        });
        pendingReplies.current = pendingReplies.current.filter((reply) => {
          // A transient read failure must not discard a sent reply; retry it on the next poll.
          if (failedIds.has(reply.taskId)) return true;
          const task = found.find((task) => task.task_id === reply.taskId);
          const gate = task?.gates.find((gate) => gate.step_id === reply.stepId);
          // A turn already running can finish and raise `turns` without taking the message just sent.
          // It stays pending while the Planner still owes a user message, or a turn failed and none has taken it.
          return !!task && isActiveTask(task) && !!gate && isGateWaiting(gate)
            && (gate.waiting_on === "planner" || gate.not_delivered > 0);
        });
        setReplies(pendingReplies.current);
        setTasks((current) => [...found, ...current.filter((task) => failedIds.has(task.task_id))]);
        // Needs-you counts tasks, including those waiting for the Planner, not user gates.
        const readTasks = pageIds.length + found.filter((task) =>
          !pageIds.includes(task.task_id) && task.group === "needs_you").length;
        setMoreTasks(Math.max(0, Object.values(page.counts).reduce((sum, n) => sum + n, 0) - readTasks));
        setError(errors[0] ?? "");
      } catch (err) {
        if (request === latest.current) setError(errorText(err));
      }
    };
    const drain = async () => {
      if (loading.current) return;
      loading.current = true;
      try {
        while (queued.current) {
          const read = queued.current;
          queued.current = null;
          await read();
        }
      } finally {
        loading.current = false;
      }
    };
    void drain();
  }, [needsYou, revision, refresh]);

  const waiting = tasks.flatMap((task) => !isActiveTask(task) ? [] : task.gates
    .filter((gate) => isGateWaiting(gate) && (gateWaitsOn(gate, isActiveTask(task)) === "user"
      || replies.some((reply) => reply.taskId === task.task_id && reply.stepId === gate.step_id)))
    .map((gate) => ({ task, gate })))
    .sort((a, b) => (Date.parse(b.gate.opened_at ?? "") || 0) - (Date.parse(a.gate.opened_at ?? "") || 0));
  const replyByGate = new Map(replies.map((reply) => [gateKey(reply.taskId, reply.stepId), reply]));
  // Keep pending receipts in the bounded card list when newer waiting gates arrive.
  waiting.sort((a, b) => Number(replyByGate.has(gateKey(b.task.task_id, b.gate.step_id)))
    - Number(replyByGate.has(gateKey(a.task.task_id, a.gate.step_id))));
  const more = Math.max(0, waiting.length - LIMIT);
  const failure = error && <p role="alert" className="text-xs text-destructive [overflow-wrap:anywhere]">
    Cannot refresh waiting gates ({error}). <a href="#tasks?group=needs_you" className={linkClass}>View tasks needing you</a>
  </p>;
  if (!waiting.length && !moreTasks) return failure || null;
  return (
    <section aria-label="Waiting for you" className="space-y-3">
      <h2 className="text-sm font-semibold">Waiting for you</h2>
      {failure}
      <div className="grid min-w-0 gap-3 lg:grid-cols-2">
        {waiting.slice(0, LIMIT).map(({ task, gate }) => (
          <GateReply key={`${task.task_id}/${gate.step}`} task={task} gate={gate}
            reply={replyByGate.get(gateKey(task.task_id, gate.step_id))} onChanged={(updated, text) => {
              // A poll started before the send must not restore the previous turn.
              latest.current++;
              pendingReplies.current = [
                ...pendingReplies.current.filter((reply) => reply.taskId !== task.task_id || reply.stepId !== updated.step_id),
                { taskId: task.task_id, stepId: updated.step_id, text },
              ];
              setReplies(pendingReplies.current);
              setTasks((current) => current.map((t) => t.task_id !== task.task_id ? t : {
                ...t, gates: t.gates.map((g) => g.step_id === updated.step_id ? updated : g),
              }));
              setRefresh((n) => n + 1);
            }} />
        ))}
      </div>
      {more > 0 && <a href="#tasks?group=needs_you" className={`inline-flex min-h-11 items-center text-sm ${linkClass}`}>
        and {more} more
      </a>}
      {moreTasks > 0 && <a href="#tasks?group=needs_you" className={`inline-flex min-h-11 items-center text-sm ${linkClass}`}>
        View {moreTasks} more {moreTasks === 1 ? "task" : "tasks"} needing you
      </a>}
    </section>
  );
}
