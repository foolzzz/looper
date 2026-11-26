import { useEffect, useRef, useState, type MouseEvent } from "react";
import { Button } from "@/components/ui/button";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { RUN_STATUS } from "@/lib/schedules";
import { TaskStatusMark } from "@/pages/tasks";
import { fetchTasks, type ScheduleRunStatus } from "@/lib/api";
import { errorText } from "@/lib/use-action";
import { formatWhen, taskTitle, workStorage, workStatusLabel, type WorkSchedule, type WorkTask } from "@/lib/work";

const ROW = "flex w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md px-3 py-2 text-left text-sm transition-colors duration-150 motion-reduce:transition-none pointer-coarse:min-h-11 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

const TASK_STATUS: Record<string, { label: string; color: string }> = {
  draft: { label: "草稿", color: "bg-muted-foreground" },
  init: { label: "待开始", color: "bg-muted-foreground" },
  running: { label: "运行中", color: "bg-primary" },
  assigned: { label: "已分配", color: "bg-primary" },
  planning: { label: "规划中", color: "bg-primary" },
  executing: { label: "执行中", color: "bg-primary" },
  verifying: { label: "验证中", color: "bg-primary" },
  stopping: { label: "停止中", color: "bg-warning" },
  waiting: { label: "等待中", color: "bg-muted-foreground" },
  pending: { label: "排队中", color: "bg-warning" },
  waiting_user: { label: "等你回复", color: "bg-warning" },
  awaiting_budget: { label: "等你决定花费", color: "bg-warning" },
  completed: { label: "已完成", color: "bg-success" },
  failed: { label: "失败", color: "bg-destructive" },
  stopped: { label: "已停止", color: "bg-warning" },
  cancelled: { label: "已取消", color: "bg-muted-foreground" },
};

export function isPlainClick(event: MouseEvent): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

export function WorkTaskLink({
  task,
  current,
  onActivate,
}: {
  task: WorkTask;
  current: boolean;
  onActivate: () => void;
}) {
  const status = task.needs_you && task.waiting_gates > 0
    ? TASK_STATUS.waiting_user : TASK_STATUS[task.status] ?? { label: task.status, color: "bg-muted-foreground" };
  return (
    <a
      href={`#work/task/${encodeURIComponent(task.task_id)}`}
      aria-current={current ? "page" : undefined}
      onClick={(event) => {
        if (!isPlainClick(event)) return;
        event.preventDefault();
        onActivate();
      }}
      className={`${ROW} ${current ? "bg-primary/10 text-primary" : "hover:bg-accent"}`}
    >
      <span data-task-title="" className="w-full min-w-0 truncate text-left font-medium">{taskTitle(task.title, task.created_at, task.task_id)}</span>
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
        <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${status.color}`} />
        {status.label}{task.waiting_gates > 0 && ` (${task.waiting_gates})`}
      </span>
      {task.unseen && (
        <span className="inline-flex items-center gap-1 text-xs">
          <span aria-hidden="true" className="h-2 w-2 rounded-full bg-primary" />
          未读
        </span>
      )}
    </a>
  );
}

export function WorkScheduleRow({
  schedule,
  onOpenTask,
  onRestored,
}: {
  schedule: WorkSchedule;
  onOpenTask: (taskId: string) => void;
  onRestored?: (id: string) => void;
}) {
  const [open, setOpen] = useState(() => workStorage.expanded().includes(schedule.schedule_id));
  const [runs, setRuns] = useState<{ task_id: string; name: string; status: string }[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [ready, setReady] = useState(false);
  const [retry, setRetry] = useState(0);
  const pages = useRef(0);
  const busy = useRef(false);
  const key = `schedule:${schedule.schedule_id}`;
  useEffect(() => {
    if (!open || ready) return;
    let live = true;
    busy.current = true;
    setLoading(true);
    setError("");
    (async () => {
      const rows: typeof runs = [];
      let before: string | null = null;
      let count = 0;
      do {
        const page = await fetchTasks({ scheduleId: schedule.schedule_id, limit: 10, before });
        if (!live) return;
        rows.push(...page.tasks);
        before = page.next_before;
        count++;
      } while (before && count < workStorage.pages(key));
      if (!live) return;
      pages.current = count;
      setRuns(rows);
      setNext(before);
      setReady(true);
    })().catch((err: unknown) => { if (live) setError(errorText(err)); })
      .finally(() => { if (live) { setLoading(false); busy.current = false; } });
    return () => { live = false; };
  }, [open, ready, schedule.schedule_id, key, retry]);
  useEffect(() => {
    if (!open || ready || error) onRestored?.(schedule.schedule_id);
  }, [open, ready, error, onRestored, schedule.schedule_id]);
  const more = async () => {
    if (!next || busy.current) return;
    busy.current = true;
    setLoading(true);
    setError("");
    try {
      const page = await fetchTasks({ scheduleId: schedule.schedule_id, limit: 10, before: next });
      setRuns(rows => [...rows, ...page.tasks]);
      setNext(page.next_before);
      workStorage.setPages(key, ++pages.current);
    } catch (err) { setError(errorText(err)); }
    finally { setLoading(false); busy.current = false; }
  };

  return (
    <div className="border-b border-border">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          const expanded = workStorage.expanded().filter(id => id !== schedule.schedule_id);
          if (!open) expanded.push(schedule.schedule_id);
          workStorage.setExpanded(expanded);
          setOpen(!open);
        }}
        className={`${ROW} hover:bg-accent`}
      >
        <span className="min-w-[7rem] flex-1 truncate text-left font-medium">{schedule.name}</span>
        {schedule.project_name && <span className="text-xs text-muted-foreground">{schedule.project_name}</span>}
        {schedule.last_status
          ? <Badge variant={RUN_STATUS[schedule.last_status as ScheduleRunStatus]?.variant ?? "outline"} className="shrink-0">{workStatusLabel(schedule.last_status)}</Badge>
          : <span className="text-xs text-muted-foreground">尚未运行</span>}
        {schedule.last_ended_at && <time dateTime={schedule.last_ended_at} className="text-xs text-muted-foreground">{formatWhen(schedule.last_ended_at)}</time>}
        {schedule.consecutive_failures > 0 && (
          <span className="text-xs">连续失败 {schedule.consecutive_failures} 次</span>
        )}
        {schedule.running && (
          <span className="inline-flex items-center gap-1 text-xs">
            <Loader2 aria-hidden="true" className="h-3.5 w-3.5 motion-safe:animate-spin" />
            运行中
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-1 px-3 pb-2">
          {error && <div role="alert"><p className="text-xs text-destructive">{error}</p>
            <Button variant="outline" onClick={() => ready ? void more() : setRetry(n => n + 1)}>重试</Button></div>}
          {loading && <p role="status" className="text-xs text-muted-foreground">正在加载…</p>}
          {runs?.map((run) => (
            <a
              key={run.task_id}
              href={`#work/task/${encodeURIComponent(run.task_id)}`}
              onClick={(event) => {
                if (!isPlainClick(event)) return;
                event.preventDefault();
                onOpenTask(run.task_id);
              }}
              className="flex items-center gap-2 rounded-md px-2 py-1 text-sm pointer-coarse:min-h-11 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <span className="min-w-0 flex-1 truncate">{taskTitle(run.name)}</span>
              <TaskStatusMark status={run.status} label={workStatusLabel(run.status)} />
            </a>
          ))}
          {next && <Button variant="outline" disabled={loading} onClick={() => void more()}>加载更多</Button>}
          {ready && runs.length === 0 && <p className="text-xs text-muted-foreground">尚未运行</p>}
        </div>
      )}
    </div>
  );
}
