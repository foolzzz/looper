import { useEffect, useState, type MouseEvent } from "react";
import { Loader2 } from "lucide-react";
import { RunStatusBadge } from "@/components/project-schedules";
import { TaskStatusMark } from "@/pages/tasks";
import { fetchTasks, type ScheduleRunStatus } from "@/lib/api";
import { errorText } from "@/lib/use-action";
import { formatWhen, taskTitle, type WorkSchedule, type WorkTask } from "@/lib/work";

const ROW = "flex w-full flex-wrap items-center gap-x-2 gap-y-1 rounded-md px-3 py-2 text-left text-sm transition-colors duration-150 motion-reduce:transition-none pointer-coarse:min-h-11 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";

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
      <span className="min-w-0 flex-1 truncate text-left font-medium">{taskTitle(task.title)}</span>
      <TaskStatusMark status={task.status} />
      {task.needs_you && <span className="text-xs">Waits for you ({task.waiting_gates})</span>}
      {task.unseen && (
        <span className="inline-flex items-center gap-1 text-xs">
          <span aria-hidden="true" className="h-2 w-2 rounded-full bg-primary" />
          Unseen
        </span>
      )}
    </a>
  );
}

export function WorkScheduleRow({
  schedule,
  onOpenTask,
}: {
  schedule: WorkSchedule;
  onOpenTask: (taskId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [runs, setRuns] = useState<{ task_id: string; name: string; status: string }[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    let live = true;
    fetchTasks({ scheduleId: schedule.schedule_id, limit: 10 })
      .then((page) => {
        if (live) setRuns(page.tasks.map((task) => ({ task_id: task.task_id, name: task.name, status: task.status })));
      })
      .catch((err: unknown) => { if (live) setError(errorText(err)); });
    return () => { live = false; };
  }, [open, schedule.schedule_id]);

  return (
    <div className="border-b border-border">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className={`${ROW} hover:bg-accent`}
      >
        <span className="min-w-[7rem] flex-1 truncate text-left font-medium">{schedule.name}</span>
        {schedule.project_name && <span className="text-xs text-muted-foreground">{schedule.project_name}</span>}
        {schedule.last_status
          ? <RunStatusBadge status={schedule.last_status as ScheduleRunStatus} />
          : <span className="text-xs text-muted-foreground">No runs yet</span>}
        {schedule.last_ended_at && <time dateTime={schedule.last_ended_at} className="text-xs text-muted-foreground">{formatWhen(schedule.last_ended_at)}</time>}
        {schedule.consecutive_failures > 0 && (
          <span className="text-xs">{schedule.consecutive_failures} consecutive failures</span>
        )}
        {schedule.running && (
          <span className="inline-flex items-center gap-1 text-xs">
            <Loader2 aria-hidden="true" className="h-3.5 w-3.5 motion-safe:animate-spin" />
            Running
          </span>
        )}
      </button>
      {open && (
        <div className="space-y-1 px-3 pb-2">
          {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
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
              <TaskStatusMark status={run.status} />
            </a>
          ))}
          {runs && runs.length === 0 && <p className="text-xs text-muted-foreground">No runs yet</p>}
        </div>
      )}
    </div>
  );
}
