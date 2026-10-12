import { useEffect, useState } from "react";
import {
  XCircle,
  Trash2,
  RefreshCw,
  Clock,
  CheckCircle2,
  AlertCircle,
  Loader2,
  Play,
  Pause,
  Plus,
  Settings2,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { fetchTasks, cancelTask, deleteTask, stopTask, startTask, createTask, type TaskInfo } from "@/lib/api";
import { TaskDetailPage } from "./task-detail";

const STATUS_FILTERS = ["all", "init", "running", "pending", "completed", "failed", "cancelled", "stopped"] as const;
type StatusFilter = (typeof STATUS_FILTERS)[number];

function statusBadge(status: string) {
  switch (status) {
    case "running":
      return <Badge variant="default">Running</Badge>;
    case "pending":
      return <Badge variant="warning">Pending</Badge>;
    case "completed":
      return <Badge variant="success">Completed</Badge>;
    case "failed":
      return <Badge variant="destructive">Failed</Badge>;
    case "cancelled":
      return <Badge variant="outline">Cancelled</Badge>;
    case "stopped":
      return <Badge variant="warning">Stopped</Badge>;
    case "init":
      return <Badge variant="outline">Init</Badge>;
    case "executing":
      return <Badge variant="default">Executing</Badge>;
    default:
      return <Badge variant="outline">{status}</Badge>;
  }
}

function statusIcon(status: string) {
  switch (status) {
    case "running":
    case "executing":
    case "assigned":
      return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
    case "pending":
      return <Clock className="h-4 w-4 text-warning" />;
    case "completed":
      return <CheckCircle2 className="h-4 w-4 text-success" />;
    case "failed":
      return <AlertCircle className="h-4 w-4 text-destructive" />;
    case "stopped":
      return <Pause className="h-4 w-4 text-warning" />;
    case "init":
      return <Settings2 className="h-4 w-4 text-muted-foreground" />;
    default:
      return <Clock className="h-4 w-4 text-muted-foreground" />;
  }
}

const ACTIVE_STATUSES = ["running", "pending", "executing", "assigned"];
const RESTARTABLE_STATUSES = ["stopped", "failed", "cancelled", "init"];

function formatTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function durationLabel(created: string | null | undefined, completed: string | null | undefined): string | null {
  if (!created || !completed) return null;
  const ms = new Date(completed).getTime() - new Date(created).getTime();
  if (isNaN(ms) || ms < 0) return null;
  if (ms < 1000) return `${ms}ms`;
  const sec = Math.round(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const rem = sec % 60;
  return rem > 0 ? `${min}m ${rem}s` : `${min}m`;
}

interface TasksPageProps {
  sub?: string;
  onNavigate?: (sub: string) => void;
}

export function TasksPage({ sub, onNavigate }: TasksPageProps) {
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [newReq, setNewReq] = useState("");
  const [newWorkDir, setNewWorkDir] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  const selectedTaskId = sub || null;
  const setSelectedTaskId = (id: string | null) => onNavigate?.(id || "");

  const load = () => {
    fetchTasks()
      .then((data) => {
        setTasks(Array.isArray(data) ? data : (data as any).tasks || []);
        setError("");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, []);

  const handleCancel = async (taskId: string) => {
    try {
      await cancelTask(taskId);
      load();
    } catch {
      // ignore
    }
  };

  const handleStop = async (taskId: string) => {
    try {
      await stopTask(taskId);
      load();
    } catch {
      // ignore
    }
  };

  const handleStart = async (taskId: string) => {
    try {
      await startTask(taskId);
      load();
    } catch {
      // ignore
    }
  };

  const handleDelete = async (taskId: string) => {
    const task = tasks.find((t) => t.task_id === taskId);
    if (task && ACTIVE_STATUSES.includes(task.status)) {
      await handleStop(taskId);
      await new Promise((r) => setTimeout(r, 500));
    }
    try {
      await deleteTask(taskId);
      setConfirmDelete(null);
      load();
    } catch {
      // ignore
    }
  };

  const handleCreate = async () => {
    if (!newReq.trim() || !newWorkDir.trim()) return;
    setCreating(true);
    setCreateError("");
    try {
      const result = await createTask({
        requirement: newReq.trim(),
        work_dir: newWorkDir.trim(),
      });
      setShowCreate(false);
      setNewReq("");
      setNewWorkDir("");
      load();
      setSelectedTaskId(result.task_id);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const filtered = filter === "all" ? tasks : tasks.filter((t) => t.status === filter);

  const statusCounts = tasks.reduce<Record<string, number>>((acc, t) => {
    acc[t.status] = (acc[t.status] || 0) + 1;
    return acc;
  }, {});

  if (selectedTaskId) {
    return (
      <TaskDetailPage
        taskId={selectedTaskId}
        onBack={() => setSelectedTaskId(null)}
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Tasks</h1>
          <p className="text-sm text-muted-foreground mt-1">{tasks.length} total tasks</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-2 rounded-lg bg-primary px-3 py-2 text-sm text-primary-foreground hover:bg-primary/90 transition-colors"
          >
            <Plus className="h-4 w-4" />
            New Task
          </button>
          <button
            type="button"
            onClick={load}
            className="flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            <RefreshCw className="h-4 w-4" />
            Refresh
          </button>
        </div>
      </div>

      {/* Status filter tabs */}
      <div className="flex items-center gap-1 flex-wrap">
        {STATUS_FILTERS.map((f) => {
          const count = f === "all" ? tasks.length : statusCounts[f] || 0;
          return (
            <button
              key={f}
              type="button"
              onClick={() => setFilter(f)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                filter === f
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground hover:text-foreground"
              }`}
            >
              {f === "all" ? "All" : f.charAt(0).toUpperCase() + f.slice(1)}
              {count > 0 && <span className="ml-1.5 opacity-70">{count}</span>}
            </button>
          );
        })}
      </div>

      {error && !tasks.length && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && !tasks.length ? (
        <div className="flex h-64 items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex h-64 items-center justify-center">
          <p className="text-sm text-muted-foreground">
            {filter === "all" ? "No tasks yet" : `No ${filter} tasks`}
          </p>
        </div>
      ) : (
        <div className="rounded-lg border border-border bg-card divide-y divide-border">
          {filtered.map((task) => (
            <div
              key={task.task_id}
              className="px-5 py-4 cursor-pointer hover:bg-muted/50 transition-colors"
              onClick={() => setSelectedTaskId(task.task_id)}
            >
              <div className="flex items-start justify-between gap-4">
                <div className="flex items-start gap-3 min-w-0">
                  {statusIcon(task.status)}
                  <div className="min-w-0">
                    <p className="text-sm font-medium truncate">{task.requirement}</p>
                    <div className="mt-1.5 flex items-center gap-2 flex-wrap">
                      {statusBadge(task.status)}
                      <span className="text-xs text-muted-foreground">
                        {formatTime(task.created_at)}
                      </span>
                      {task.completed_at && (
                        <span className="text-xs text-muted-foreground">
                          → {formatTime(task.completed_at)}
                        </span>
                      )}
                      {durationLabel(task.created_at, task.completed_at) && (
                        <span className="text-xs text-muted-foreground font-mono">
                          ({durationLabel(task.created_at, task.completed_at)})
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="shrink-0 flex items-center gap-1">
                  {ACTIVE_STATUSES.includes(task.status) && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleStop(task.task_id);
                      }}
                      className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-warning hover:bg-warning/10 transition-colors"
                    >
                      <Pause className="h-3.5 w-3.5" />
                      Stop
                    </button>
                  )}
                  {RESTARTABLE_STATUSES.includes(task.status) && (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        handleStart(task.task_id);
                      }}
                      className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-success hover:bg-success/10 transition-colors"
                    >
                      <Play className="h-3.5 w-3.5" />
                      Start
                    </button>
                  )}
                  {confirmDelete === task.task_id ? (
                    <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                      <button
                        type="button"
                        onClick={() => handleDelete(task.task_id)}
                        className="rounded-md px-2 py-1 text-xs bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-colors"
                      >
                        Confirm
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmDelete(null)}
                        className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                      >
                        No
                      </button>
                    </div>
                  ) : (
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        setConfirmDelete(task.task_id);
                      }}
                      className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Create task modal */}
      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="w-full max-w-lg rounded-xl border border-border bg-card p-6 shadow-xl">
            <h2 className="text-lg font-semibold mb-4">Create New Task</h2>
            <div className="space-y-4">
              <div>
                <label className="text-sm font-medium text-muted-foreground">Requirement *</label>
                <textarea
                  value={newReq}
                  onChange={(e) => setNewReq(e.target.value)}
                  placeholder="Describe what this task should accomplish..."
                  rows={3}
                  className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
                />
              </div>
              <div>
                <label className="text-sm font-medium text-muted-foreground">Work Directory *</label>
                <input
                  type="text"
                  value={newWorkDir}
                  onChange={(e) => setNewWorkDir(e.target.value)}
                  placeholder="~/projects/my-app"
                  className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/50"
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  Where the task's Workers run: an absolute path or one starting with ~/. Created if it does not exist.
                </p>
              </div>
              <p className="text-xs text-muted-foreground">
                Task will be created in <strong>init</strong> status. You can review and edit the configuration before starting.
              </p>
              {createError && <p className="text-xs text-destructive">{createError}</p>}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setShowCreate(false);
                    setCreateError("");
                    setNewReq("");
                    setNewWorkDir("");
                  }}
                  className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleCreate}
                  disabled={!newReq.trim() || !newWorkDir.trim() || creating}
                  className="flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
                >
                  {creating && <Loader2 className="h-4 w-4 animate-spin" />}
                  Create
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
