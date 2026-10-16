import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  Clock,
  AlertCircle,
  Loader2,
  FileCode,
  MessageSquare,
  Activity,
  Terminal,
  Bot,
  User,
  GitBranch,
  Play,
  Pause,
  Settings2,
  Save,
  FolderOpen,
  Folder,
  History,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ClampedText } from "@/components/clamped-text";
import { Markdown } from "@/components/markdown";
import { ScheduledBadge } from "@/components/scheduled-badge";
import { historyHref } from "@/pages/agent-history";
import { TaskCostBreakdown } from "@/components/task-cost";
import { ContinueAtCap, TaskSpendMeter } from "@/components/task-budget";
import { TaskConcurrencySection } from "@/components/task-concurrency";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { costBasis, costCaveats, formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";
import {
  ReactFlow,
  Background,
  type Node,
  type Edge,
  Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useProjects } from "@/lib/use-projects";
import {
  fetchTaskDetail,
  fetchTaskActivity,
  fetchTaskConversation,
  streamTaskActivity,
  stopTask,
  isActiveTask,
  startTask,
  startReadyTask,
  READY_LABEL,
  READY_STATUS,
  updateTaskConfig,
  editTaskRequirement,
  setTaskProject,
  fetchKnowledgeDrafts,
  type KnowledgeDraft,
  type TaskConcurrency,
  type TaskDetail,
  type Supplement,
  type ManagerAction,
  type TaskStep,
  type JournalEntry,
  type ProgressNote,
  type ActivityEvent,
} from "@/lib/api";

type Tab = "overview" | "pipeline" | "sessions" | "conversation";

function stepStatusIcon(status: string) {
  switch (status) {
    case "dispatched":
    case "running":
      return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
    case "verified":
    case "completed":
      return <CheckCircle2 className="h-4 w-4 text-success" />;
    case "failed":
      return <AlertCircle className="h-4 w-4 text-destructive" />;
    default:
      return <Clock className="h-4 w-4 text-muted-foreground" />;
  }
}

function stepStatusBadge(status: string) {
  switch (status) {
    case "dispatched":
      return <Badge variant="default">Dispatched</Badge>;
    case "running":
      return <Badge variant="default">Running</Badge>;
    case "verified":
      return <Badge variant="success">Verified</Badge>;
    case "completed":
      return <Badge variant="success">Completed</Badge>;
    case "failed":
      return <Badge variant="destructive">Failed</Badge>;
    case "skipped":
      return <Badge variant="outline">Skipped</Badge>;
    default:
      return <Badge variant="outline">Pending</Badge>;
  }
}

function taskStatusBadge(status: string) {
  switch (status) {
    case "running":
      return <Badge variant="default" className="text-sm">Running</Badge>;
    case "completed":
      return <Badge variant="success" className="text-sm">Completed</Badge>;
    case "failed":
      return <Badge variant="destructive" className="text-sm">Failed</Badge>;
    case "cancelled":
      return <Badge variant="outline" className="text-sm">Cancelled</Badge>;
    case "pending":
      return <Badge variant="warning" className="text-sm">Pending</Badge>;
    case READY_STATUS:
      return <Badge variant="outline" className="text-sm">{READY_LABEL}</Badge>;
    default:
      return <Badge variant="outline" className="text-sm">{status}</Badge>;
  }
}

const PHASE_CONFIG: Record<string, { label: string; icon: typeof Clock; color: string }> = {
  pending: { label: "Pending", icon: Clock, color: "text-muted-foreground" },
  planning: { label: "Planning", icon: Bot, color: "text-blue-500" },
  executing: { label: "Worker Executing", icon: Terminal, color: "text-primary" },
  verifying: { label: "Verifying", icon: CheckCircle2, color: "text-amber-500" },
  completed: { label: "Completed", icon: CheckCircle2, color: "text-success" },
  failed: { label: "Failed", icon: AlertCircle, color: "text-destructive" },
};

function phaseBadge(phase: string) {
  const cfg = PHASE_CONFIG[phase] || PHASE_CONFIG.pending;
  const Icon = cfg.icon;
  return (
    <div className="flex items-center gap-1.5">
      {(phase === "planning" || phase === "executing") ? (
        <Loader2 className={cn("h-4 w-4 animate-spin", cfg.color)} />
      ) : (
        <Icon className={cn("h-4 w-4", cfg.color)} />
      )}
      <span className={cn("text-sm font-medium", cfg.color)}>{cfg.label}</span>
    </div>
  );
}

function activityKindIcon(kind: string) {
  switch (kind) {
    case "tool_use":
      return <Terminal className="h-3.5 w-3.5 text-primary" />;
    case "text":
      return <MessageSquare className="h-3.5 w-3.5 text-foreground" />;
    case "error":
      return <AlertCircle className="h-3.5 w-3.5 text-destructive" />;
    case "phase":
      return <Activity className="h-3.5 w-3.5 text-muted-foreground" />;
    default:
      return <Activity className="h-3.5 w-3.5 text-muted-foreground" />;
  }
}

function formatTime(iso: string | null) {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

/** Wall-clock time (24h, seconds) for rows within one task; the date is in the tooltip. */
function formatClock(iso: string | null) {
  if (!iso) return "--:--:--";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleTimeString([], { hour12: false });
}

function formatDuration(start: string | null, end: string | null) {
  if (!start) return "-";
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  const sec = Math.floor((e - s) / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  return `${min}m ${sec % 60}s`;
}

// --- Overview Tab ---

function LiveActivityFeed({ taskId, isActive }: { taskId: string; isActive: boolean }) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [live, setLive] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const { activity } = await fetchTaskActivity(taskId, 0, 200);
        if (cancelled) return;
        setEvents(activity);
      } catch {
        // ignore
      }

      if (!isActive) return;

      const maxSeq = events.length > 0 ? Math.max(...events.map((e) => e.seq)) : 0;
      setLive(true);
      cleanupRef.current = streamTaskActivity(
        taskId,
        maxSeq,
        (event) => {
          if (cancelled) return;
          setEvents((prev) => {
            if (prev.some((e) => e.seq === event.seq)) return prev;
            return [...prev, event];
          });
        },
        () => setLive(false),
      );
    })();

    return () => {
      cancelled = true;
      cleanupRef.current?.();
    };
  }, [taskId, isActive]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [events]);

  if (events.length === 0 && !isActive) return null;

  return (
    <div className="rounded-lg border border-border bg-card overflow-hidden">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <Activity className="h-3.5 w-3.5 text-muted-foreground" />
        <span className="text-xs font-medium">Live Activity</span>
        <span className="text-[10px] text-muted-foreground">{events.length} events</span>
        {live && (
          <span className="flex items-center gap-1 text-xs text-success ml-auto">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-75" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-success" />
            </span>
            Streaming
          </span>
        )}
      </div>
      <div ref={scrollRef} className="max-h-[300px] overflow-y-auto p-3 space-y-1">
        {events.length === 0 && (
          <div className="flex items-center justify-center py-4">
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground mr-2" />
            <span className="text-xs text-muted-foreground">Waiting for activity...</span>
          </div>
        )}
        {events.map((ev) => (
          <div
            key={ev.seq}
            className={cn(
              "flex items-start gap-2 rounded px-2 py-1.5 text-xs",
              ev.kind === "error" ? "bg-destructive/10" : "hover:bg-muted/50",
            )}
          >
            {activityKindIcon(ev.kind)}
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-medium text-muted-foreground">
                  {ev.worker || "system"}
                </span>
                <span className="text-[10px] text-muted-foreground/60">
                  {formatTime(ev.created_at)}
                </span>
              </div>
              <p className={cn(
                "mt-0.5 whitespace-pre-wrap break-all",
                ev.kind === "error" && "text-destructive",
              )}>
                {ev.summary || ev.kind}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/** The config text as an object, or null when it is not a JSON object. */
function parseConfig(text: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(text.trim() || "{}");
    return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/** How a change came, in words. */
function viaLabel(via: string): string {
  if (via === "chat") return "Chat";
  if (via === "task_page") return "task page";
  if (via === "created") return "created";
  if (via.startsWith("manager:")) return `Manager (${via.slice("manager:".length)})`;
  return via || "—";
}

/** A started task's requirement: the current text, its earlier versions, and an edit its Planner gets in a turn. */
function TaskRequirement({ task }: { task: TaskDetail }) {
  const versions = task.requirement_versions ?? [];
  // Queued or running, whatever its Planner is doing; one not started edits it above.
  const editable = isActiveTask(task);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(task.requirement);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const save = async () => {
    setSaving(true);
    setError("");
    try {
      await editTaskRequirement(task.task_id, text);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mt-1 space-y-2">
      {editing ? (
        <>
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            aria-label="Requirement"
            className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
          />
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={save}
              disabled={saving || !text.trim() || text === task.requirement}
              className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
            >
              {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
              Save
            </button>
            <button
              type="button"
              onClick={() => { setEditing(false); setText(task.requirement); setError(""); }}
              className="rounded-md border border-border px-3 py-1 text-xs text-muted-foreground hover:text-foreground"
            >
              Cancel
            </button>
            <span className="text-xs text-muted-foreground">The Planner gets the change and decides whether to re-plan.</span>
          </div>
        </>
      ) : (
        <div className="flex items-start justify-between gap-3">
          <p className="text-sm whitespace-pre-wrap">{task.requirement}</p>
          {editable && (
            <button
              type="button"
              onClick={() => { setText(task.requirement); setEditing(true); }}
              className="shrink-0 rounded-md border border-border px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground"
            >
              Edit
            </button>
          )}
        </div>
      )}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {versions.length > 1 && (
        <details className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">History ({versions.length} versions)</summary>
          <ol className="mt-2 space-y-2">
            {[...versions].reverse().map((v) => (
              <li key={v.version} className="rounded-md border border-border px-3 py-2">
                <p className="text-muted-foreground">
                  Version {v.version} · {viaLabel(v.via)} · {new Date(v.created_at).toLocaleString()}
                  {v.version > 1 && !v.delivered_at && " · not yet seen by the Planner"}
                </p>
                <p className="mt-1 whitespace-pre-wrap text-foreground">{v.text}</p>
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}

/** What the user added to the task while it ran, oldest first; every later Worker and Reviewer sees them. */
function TaskSupplements({ supplements }: { supplements: Supplement[] }) {
  if (!supplements.length) return null;
  return (
    <div>
      <label className="text-xs font-medium text-muted-foreground">Supplements ({supplements.length})</label>
      <ol className="mt-1 space-y-2">
        {supplements.map((s, i) => (
          <li key={`${s.created_at}-${i}`} className="rounded-md border border-border px-3 py-2 text-sm">
            <p className="whitespace-pre-wrap">{s.text}</p>
            <p className="mt-1 text-xs text-muted-foreground">
              {viaLabel(s.via)} · {new Date(s.created_at).toLocaleString()}
              {!s.delivered_at && " · not yet seen by the Planner"}
            </p>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** What a Manager action did, in a few words: its tool's action, or the tool. */
function managerActionLabel(action: ManagerAction): string {
  const tool = action.tool.replace(/^looper_/, "").replace(/_/g, " ");
  const verb = typeof action.arguments.action === "string" ? `: ${action.arguments.action}` : "";
  return `${tool}${verb}`;
}

/** What the Manager did to the task through its tools, newest first ("由管家操作"). */
function TaskManagerActions({ actions }: { actions: ManagerAction[] }) {
  if (!actions.length) return null;
  return (
    <div>
      <label className="text-xs font-medium text-muted-foreground">By the Manager (由管家操作)</label>
      <ul className="mt-1 space-y-1 text-xs">
        {actions.map((a, i) => (
          <li key={`${a.created_at}-${i}`} className="flex flex-wrap items-center gap-x-2 text-muted-foreground">
            <span className="text-foreground">{managerActionLabel(a)}</span>
            {a.status === "needs_confirmation" ? (
              <Badge variant="outline">asked to confirm</Badge>
            ) : a.confirmation_id ? (
              <Badge variant="outline">confirmed</Badge>
            ) : null}
            <span>
              {a.channel || "—"} · {new Date(a.created_at).toLocaleString()}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function TaskConfigSection({ task }: { task: TaskDetail }) {
  const isInit = task.status === "init";
  const [editReq, setEditReq] = useState(task.requirement);
  const [editWorkDir, setEditWorkDir] = useState(task.work_dir || "");
  const [editConfig, setEditConfig] = useState(JSON.stringify(task.config || {}, null, 2));
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    setEditReq(task.requirement);
    setEditWorkDir(task.work_dir || "");
    setEditConfig(JSON.stringify(task.config || {}, null, 2));
    setDirty(false);
  }, [task.requirement, task.work_dir, task.config]);

  const handleSave = async () => {
    if (!editWorkDir.trim()) {
      setSaveError("A work directory is required.");
      return;
    }
    const parsedConfig = parseConfig(editConfig);
    if (!parsedConfig) {
      setSaveError("Config must be a JSON object, such as {}.");
      return;
    }
    setSaving(true);
    setSaveError("");
    try {
      await updateTaskConfig(task.task_id, {
        requirement: editReq !== task.requirement ? editReq : undefined,
        work_dir: editWorkDir !== (task.work_dir || "") ? editWorkDir : undefined,
        config: parsedConfig,
      });
      setDirty(false);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold flex items-center gap-1.5">
          <Settings2 className="h-4 w-4" />
          Task Configuration
        </h3>
        {isInit && dirty && (
          <button
            type="button"
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-1.5 rounded-md bg-primary px-3 py-1 text-xs text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {saving ? <Loader2 className="h-3 w-3 animate-spin" /> : <Save className="h-3 w-3" />}
            Save
          </button>
        )}
      </div>
      <div className="rounded-lg border border-border bg-card p-4 space-y-4">
        <div>
          <label className="text-xs font-medium text-muted-foreground">Requirement</label>
          {isInit ? (
            <textarea
              value={editReq}
              onChange={(e) => { setEditReq(e.target.value); setDirty(true); }}
              rows={3}
              className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
            />
          ) : (
            <TaskRequirement task={task} />
          )}
        </div>
        <TaskSupplements supplements={task.supplements ?? []} />
        <TaskManagerActions actions={task.manager_actions ?? []} />
        <div>
          <label className="text-xs font-medium text-muted-foreground flex items-center gap-1">
            <FolderOpen className="h-3 w-3" />
            Work Directory
          </label>
          {isInit ? (
            <input
              type="text"
              value={editWorkDir}
              onChange={(e) => { setEditWorkDir(e.target.value); setDirty(true); }}
              placeholder="~/projects/my-app (required)"
              className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          ) : (
            <p className="mt-1 text-sm font-mono">{task.work_dir || "—"}</p>
          )}
        </div>
        <div>
          <label className="text-xs font-medium text-muted-foreground">Config (JSON)</label>
          {isInit ? (
            <textarea
              value={editConfig}
              onChange={(e) => { setEditConfig(e.target.value); setDirty(true); }}
              rows={4}
              className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-primary/50 resize-none"
            />
          ) : (
            <pre className="mt-1 text-xs font-mono whitespace-pre-wrap bg-muted rounded-lg px-3 py-2">
              {JSON.stringify(task.config || {}, null, 2)}
            </pre>
          )}
        </div>
        {saveError && <p role="alert" className="text-xs text-destructive">{saveError}</p>}
        {isInit && (
          <p className="text-xs text-muted-foreground">
            Edit the configuration above, then click <strong>Start</strong> to begin execution.
          </p>
        )}
      </div>
    </div>
  );
}

function OverviewTab({
  task,
  onContinued,
  onConcurrency,
}: {
  task: TaskDetail;
  onContinued: () => void;
  onConcurrency: (concurrency: TaskConcurrency) => void;
}) {
  const steps = task.steps || [];
  const done = steps.filter((s) => s.status === "completed").length;
  const total = steps.length;
  const isActive = isActiveTask(task);

  return (
    <div className="space-y-6 p-5">
      {task.status === "failed" && task.fail_kind === "task_budget" && (
        <ContinueAtCap
          taskId={task.task_id}
          projectName={task.project_name}
          spent={task.cost?.cost_usd ?? 0}
          cap={task.task_budget_usd}
          ownCap={task.own_budget_usd != null}
          onContinued={onContinued}
        />
      )}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-xs text-muted-foreground">Status</p>
          <div className="mt-1">{taskStatusBadge(task.status)}</div>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-xs text-muted-foreground">Phase</p>
          <div className="mt-1">{phaseBadge(task.phase || "pending")}</div>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-xs text-muted-foreground">Created</p>
          <p className="mt-1 text-sm font-medium">{formatTime(task.created_at)}</p>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-xs text-muted-foreground">Duration</p>
          <p className="mt-1 text-sm font-medium">
            {formatDuration(task.created_at, task.completed_at)}
          </p>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-xs text-muted-foreground">Steps</p>
          <p className="mt-1 text-sm font-medium">
            {total > 0 ? `${done}/${total}` : "No steps yet"}
          </p>
        </div>
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="text-xs text-muted-foreground">Cost</p>
          <p className="mt-1 font-mono text-sm font-medium" title={task.cost ? costCaveats(task.cost).join(", ") : undefined}>
            {task.cost ? formatUsd(task.cost.cost_usd) : "–"}
          </p>
          {task.cost && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              {task.cost.runs} run{task.cost.runs === 1 ? "" : "s"}
              {costBasis(task.cost) && ` · ${costBasis(task.cost)}`}
            </p>
          )}
          {task.cost && task.task_budget_usd != null && (
            <TaskSpendMeter spent={task.cost.cost_usd} cap={task.task_budget_usd} />
          )}
        </div>
      </div>

      <TaskProgress task={task} />

      {task.cost && task.cost.runs > 0 && <TaskCostBreakdown cost={task.cost} />}

      <TaskConfigSection task={task} />

      {task.concurrency && (
        <TaskConcurrencySection
          taskId={task.task_id}
          projectId={task.project_id}
          concurrency={task.concurrency}
          onSaved={onConcurrency}
        />
      )}

      {steps.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold mb-2">Steps</h3>
          <div className="rounded-lg border border-border bg-card divide-y divide-border">
            {steps.map((step: TaskStep) => (
              <div key={step.id} className="px-4 py-3">
                <div className="flex items-start gap-3">
                  {stepStatusIcon(step.status)}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-xs text-muted-foreground">#{step.step_index + 1}</span>
                      {stepStatusBadge(step.status)}
                      {step.worker_id && (
                        <a
                          href={historyHref(step.worker_id, task.task_id)}
                          className="text-xs text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground"
                          title="打开该 Worker 的运行详情（启动命令、输入 prompt、stream-json 事件）"
                        >
                          {step.worker_id}
                        </a>
                      )}
                    </div>
                    <p className="mt-1 text-sm">{step.instruction}</p>
                    {step.target_files && step.target_files.length > 0 && (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {step.target_files.map((f) => (
                          <span
                            key={f}
                            className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground"
                          >
                            <FileCode className="h-3 w-3" />
                            {f}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {task.status === "completed" && task.project_id && (
        <LessonsDraftNote taskId={task.task_id} projectId={task.project_id} />
      )}

      <TaskFindings entries={task.journal || []} />

      <AgentTimeline taskId={task.task_id} />

      <LiveActivityFeed taskId={task.task_id} isActive={isActive} />
    </div>
  );
}

// --- Progress: the Planner's notes, and the task's final summary or closing note ---

const NOTE_FIELDS: [keyof Omit<ProgressNote, "at">, string][] = [
  ["done", "Done"],
  ["now", "Now"],
  ["next", "Next"],
  ["risks", "Risks"],
  ["estimate", "Estimate"],
];

function NoteFields({ note }: { note: ProgressNote }) {
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
      {NOTE_FIELDS.map(([field, label]) => (
        <div key={field} className="contents">
          <dt className="text-muted-foreground">{label}</dt>
          <dd className="min-w-0 whitespace-pre-wrap break-words">{note[field]}</dd>
        </div>
      ))}
    </dl>
  );
}

// How an ended task's last word is shown: a completed task's final summary (its result), a failed one's closing
// note (its error), a stopped one's closing note (its result).
const ENDINGS: Record<string, { title: string; Icon: typeof CheckCircle2; box: string; label: string }> = {
  completed: { title: "Final summary", Icon: CheckCircle2, box: "border-success/30 bg-success/5", label: "text-muted-foreground" },
  failed: { title: "Closing note", Icon: AlertCircle, box: "border-destructive/30 bg-destructive/5", label: "text-destructive" },
  stopped: { title: "Closing note", Icon: Pause, box: "border-border bg-card", label: "text-muted-foreground" },
};

/**
 * Where the task stands: once it has ended, its final summary (completed) or closing note (failed, stopped) on top;
 * then the Planner's latest progress note, and the earlier ones below it. Nothing while there is neither.
 */
function TaskProgress({ task }: { task: TaskDetail }) {
  const notes = task.progress ?? [];
  const ending = ENDINGS[task.status];
  const endingText = ending && (task.status === "failed" ? task.error : task.result_summary);
  if (!endingText && notes.length === 0) return null;
  const [latest, ...earlier] = notes;
  return (
    <section aria-labelledby="task-progress-title">
      <h3 id="task-progress-title" className="mb-2 text-sm font-semibold">
        Progress
      </h3>
      <div className="space-y-3">
        {ending && endingText && (
          <div className={cn("rounded-lg border px-4 py-3", ending.box)}>
            <p className={cn("mb-2 flex items-center gap-1.5 text-xs font-medium", ending.label)}>
              <ending.Icon className="h-3.5 w-3.5" />
              {ending.title}
            </p>
            {/* One heading per field, kept below the section's own heading. */}
            <div className="[&_h2]:mb-1 [&_h2]:mt-4 [&_h2]:text-sm [&_h2]:font-semibold">
              <Markdown>{endingText}</Markdown>
            </div>
          </div>
        )}
        {latest && (
          <div className="rounded-lg border border-border bg-card px-4 py-3">
            <p className="mb-2 text-xs text-muted-foreground">Latest note from the Planner · {formatTime(latest.at)}</p>
            <NoteFields note={latest} />
          </div>
        )}
        {earlier.length > 0 && (
          <details className="rounded-lg border border-border bg-card">
            <summary className="cursor-pointer px-4 py-2 text-xs text-muted-foreground">
              Earlier notes ({earlier.length})
            </summary>
            <ol className="divide-y divide-border border-t border-border">
              {earlier.map((note) => (
                <li key={note.at} className="px-4 py-3">
                  <p className="mb-1.5 text-xs text-muted-foreground">{formatTime(note.at)}</p>
                  <NoteFields note={note} />
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </section>
  );
}

// --- Lessons: the draft a completed task left for review ---

/** When the task's lessons wait as a draft (the project's distill_lessons "draft"), a link to review it. */
function LessonsDraftNote({ taskId, projectId }: { taskId: string; projectId: string }) {
  const [draft, setDraft] = useState<KnowledgeDraft | null>(null);
  useEffect(() => {
    let alive = true;
    fetchKnowledgeDrafts(projectId)
      .then(({ drafts }) => alive && setDraft(drafts.find((d) => d.task_id === taskId) ?? null))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [taskId, projectId]);
  if (!draft) return null;
  return (
    <p className="rounded-lg border border-border bg-card px-4 py-3 text-sm">
      This task&apos;s lessons are a draft that agents do not see yet: <span className="font-medium">{draft.title}</span>.{" "}
      <a
        href={`#projects/${encodeURIComponent(projectId)}/knowledge/${encodeURIComponent(draft.draft_id)}`}
        className="font-medium text-primary underline-offset-2 hover:underline"
      >
        Review the draft
      </a>
    </p>
  );
}

// --- Findings: the task journal ---

type BadgeVariant = "default" | "success" | "warning" | "destructive" | "outline";

const JOURNAL_KINDS: Record<JournalEntry["kind"], { label: string; variant: BadgeVariant }> = {
  step_accepted: { label: "Accepted", variant: "success" },
  step_failed: { label: "Failed", variant: "destructive" },
  step_retry: { label: "Retry", variant: "warning" },
  decision: { label: "Decision", variant: "default" },
  supplement: { label: "Supplement", variant: "default" },
  session_switch: { label: "New session", variant: "outline" },
  handoff: { label: "Handoff", variant: "outline" },
  progress: { label: "Progress", variant: "outline" },
};

/** The task journal, newest first: steps accepted, failed or retried with what their Worker and Reviewer
 * learned, the Planner's decisions, the supplements, and new Planner sessions with their handoffs. */
function TaskFindings({ entries: all }: { entries: JournalEntry[] }) {
  // The progress notes have a section of their own (TaskProgress).
  const entries = all.filter((entry) => entry.kind !== "progress");
  if (entries.length === 0) return null;
  return (
    <div>
      <h3 className="text-sm font-semibold mb-2">Findings</h3>
      <ol className="rounded-lg border border-border bg-card divide-y divide-border">
        {[...entries].reverse().map((entry) => {
          const kind = JOURNAL_KINDS[entry.kind] ?? { label: entry.kind, variant: "outline" as const };
          return (
            <li key={entry.id} className="px-4 py-3">
              <div className="flex flex-wrap items-baseline gap-2">
                <Badge variant={kind.variant}>{kind.label}</Badge>
                {(entry.step_key || entry.step_id) && (
                  <span className="font-mono text-xs text-muted-foreground">{entry.step_key || entry.step_id}</span>
                )}
                <span className="text-xs text-muted-foreground">{formatTime(entry.created_at)}</span>
              </div>
              <ClampedText text={entry.summary || "(no summary)"} className="mt-1 text-sm" />
              {entry.learnings.length > 0 && (
                <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-sm text-muted-foreground">
                  {entry.learnings.map((learning, i) => (
                    <li key={i}>{learning}</li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// --- Agent Timeline ---

function AgentTimeline({ taskId }: { taskId: string }) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    fetchTaskActivity(taskId, 0, 500)
      .then(({ activity }) => setEvents(activity))
      .catch(() => {});
  }, [taskId]);

  if (events.length === 0) return null;

  const grouped = new Map<string, ActivityEvent[]>();
  for (const ev of events) {
    const key = ev.worker || "system";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key)!.push(ev);
  }

  const agentRole = (name: string) => {
    if (name.startsWith("planner")) return "Planner";
    if (name.startsWith("worker")) return "Worker";
    if (name === "system") return "System";
    return "Agent";
  };

  const agentColor = (name: string) => {
    if (name.startsWith("planner")) return "text-blue-400 bg-blue-500/10 border-blue-500/30";
    if (name.startsWith("worker")) return "text-emerald-400 bg-emerald-500/10 border-emerald-500/30";
    return "text-muted-foreground bg-muted border-border";
  };

  const toggle = (agent: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(agent)) next.delete(agent);
      else next.add(agent);
      return next;
    });
  };

  return (
    <div>
      <h3 className="text-sm font-semibold mb-2">Agent Timeline</h3>
      <div className="space-y-3">
        {Array.from(grouped.entries()).map(([agent, agentEvents]) => {
          const first = agentEvents[0];
          const last = agentEvents[agentEvents.length - 1];
          const duration = formatDuration(first.created_at, last.created_at);
          const toolUses = agentEvents.filter((e) => e.kind === "tool_use").length;
          const errors = agentEvents.filter((e) => e.kind === "error").length;
          const colors = agentColor(agent);
          const isOpen = expanded.has(agent);

          return (
            <div key={agent} className={cn("rounded-lg border overflow-hidden", colors)}>
              <div className="flex items-start">
              <button
                type="button"
                onClick={() => toggle(agent)}
                onDoubleClick={() => { window.location.hash = historyHref(agent, taskId); }}
                title="单击展开/折叠，双击打开该 agent 的运行详情（启动命令、输入 prompt、stream-json 事件）"
                className="min-w-0 flex-1 text-left px-4 py-3 hover:brightness-110 transition-all"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <span
                      className="font-mono text-xs tabular-nums opacity-70"
                      title={formatTime(first.created_at)}
                    >
                      {formatClock(first.created_at)}
                    </span>
                    <Bot className="h-4 w-4" />
                    <span className="text-sm font-medium">{agent}</span>
                    <Badge variant="outline" className="text-[10px] py-0 h-4">
                      {agentRole(agent)}
                    </Badge>
                  </div>
                  <div className="flex items-center gap-3 text-xs opacity-70">
                    <span>{agentEvents.length} events</span>
                    {toolUses > 0 && <span>{toolUses} tools</span>}
                    {errors > 0 && <span className="text-destructive">{errors} errors</span>}
                    <span>{duration}</span>
                    <span className="text-[10px]">{isOpen ? "▲" : "▼"}</span>
                  </div>
                </div>
                {!isOpen && (
                  <div className="mt-2 flex items-center gap-0.5 overflow-x-auto">
                    {agentEvents.slice(0, 40).map((ev) => (
                      <div
                        key={ev.seq}
                        title={`${ev.kind}: ${ev.summary || ""}\n${formatTime(ev.created_at)}`}
                        className={cn(
                          "h-2 w-2 rounded-full shrink-0",
                          ev.kind === "tool_use" && "bg-primary",
                          ev.kind === "text" && "bg-foreground/50",
                          ev.kind === "error" && "bg-destructive",
                          ev.kind === "phase" && "bg-amber-400",
                          !["tool_use", "text", "error", "phase"].includes(ev.kind) && "bg-muted-foreground/40",
                        )}
                      />
                    ))}
                    {agentEvents.length > 40 && (
                      <span className="text-[10px] text-muted-foreground ml-1">+{agentEvents.length - 40}</span>
                    )}
                  </div>
                )}
              </button>
              <a
                href={historyHref(agent, taskId)}
                className="m-2.5 flex shrink-0 items-center gap-1 rounded-md border border-current/30 px-2 py-1 text-xs opacity-80 hover:opacity-100 hover:bg-white/5"
                title={`Open ${agent}'s session history: launch command, full transcript and activity`}
              >
                <History className="h-3.5 w-3.5" />
                History
              </a>
              </div>
              {isOpen && (
                <div className="border-t border-inherit px-4 py-2 max-h-[400px] overflow-y-auto space-y-1">
                  {agentEvents.map((ev) => (
                    <div
                      key={ev.seq}
                      className={cn(
                        "flex items-start gap-2 rounded px-2 py-1.5 text-xs",
                        ev.kind === "error" ? "bg-destructive/10" : "hover:bg-white/5",
                      )}
                    >
                      <span
                        className="shrink-0 font-mono text-[11px] leading-4 tabular-nums text-muted-foreground"
                        title={formatTime(ev.created_at)}
                      >
                        {formatClock(ev.created_at)}
                      </span>
                      {activityKindIcon(ev.kind)}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <Badge variant="outline" className="text-[9px] py-0 h-3.5">
                            {ev.kind}
                          </Badge>
                        </div>
                        <p className={cn(
                          "mt-0.5 whitespace-pre-wrap break-all",
                          ev.kind === "error" && "text-destructive",
                        )}>
                          {ev.summary || ev.kind}
                        </p>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// --- Sessions Tab ---

function SessionsTab({ taskId }: { taskId: string }) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [live, setLive] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const cleanupRef = useRef<(() => void) | null>(null);

  const scrollToBottom = useCallback(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, []);

  useEffect(() => {
    let cancelled = false;

    (async () => {
      try {
        const { activity } = await fetchTaskActivity(taskId, 0, 500);
        if (cancelled) return;
        setEvents(activity);
      } catch {
        // ignore
      }

      const maxSeq = events.length > 0 ? Math.max(...events.map((e) => e.seq)) : 0;

      cleanupRef.current = streamTaskActivity(
        taskId,
        maxSeq,
        (event) => {
          if (cancelled) return;
          setEvents((prev) => {
            if (prev.some((e) => e.seq === event.seq)) return prev;
            return [...prev, event];
          });
        },
        () => setLive(false),
      );
    })();

    return () => {
      cancelled = true;
      cleanupRef.current?.();
    };
  }, [taskId]);

  useEffect(scrollToBottom, [events, scrollToBottom]);

  const grouped = new Map<string, ActivityEvent[]>();
  for (const ev of events) {
    const key = ev.worker || "system";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key)!.push(ev);
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <span className="text-xs text-muted-foreground">
          {events.length} events
        </span>
        {live && (
          <span className="flex items-center gap-1 text-xs text-success">
            <span className="relative flex h-1.5 w-1.5">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-success opacity-75" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-success" />
            </span>
            Live
          </span>
        )}
      </div>
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-1.5">
        {events.length === 0 && (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-muted-foreground">No activity yet</p>
          </div>
        )}
        {events.map((ev) => (
          <div
            key={ev.seq}
            className={cn(
              "flex items-start gap-2 rounded px-3 py-2 text-sm",
              ev.kind === "error" ? "bg-destructive/10" : "hover:bg-muted/50",
            )}
          >
            {activityKindIcon(ev.kind)}
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="text-xs font-medium text-muted-foreground">
                  {ev.worker || "system"}
                </span>
                <span className="text-[10px] text-muted-foreground/60">
                  {formatTime(ev.created_at)}
                </span>
              </div>
              <p className={cn(
                "mt-0.5 whitespace-pre-wrap break-all",
                ev.kind === "error" && "text-destructive",
              )}>
                {ev.summary || ev.kind}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// --- Conversation Tab ---

function ConversationTab({ taskId }: { taskId: string }) {
  const [messages, setMessages] = useState<ActivityEvent[]>([]);

  useEffect(() => {
    fetchTaskConversation(taskId)
      .then(({ conversation }) => setMessages(conversation))
      .catch(() => {});
  }, [taskId]);

  return (
    <div className="p-5 space-y-3">
      {messages.length === 0 && (
        <div className="flex h-48 items-center justify-center">
          <p className="text-sm text-muted-foreground">No conversation for this task</p>
        </div>
      )}
      {messages.map((msg) => {
        const isUser = msg.kind === "user_message";
        return (
          <div
            key={msg.seq}
            className={cn("flex gap-3", isUser ? "justify-end" : "justify-start")}
          >
            {!isUser && (
              <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/10">
                <Bot className="h-3.5 w-3.5 text-primary" />
              </div>
            )}
            <div
              className={cn(
                "max-w-[70%] rounded-lg px-4 py-2.5 text-sm",
                isUser
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-foreground",
              )}
            >
              <p className="whitespace-pre-wrap">{msg.summary || ""}</p>
              <p className="mt-1 text-[10px] opacity-60">{formatTime(msg.created_at)}</p>
            </div>
            {isUser && (
              <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent">
                <User className="h-3.5 w-3.5 text-muted-foreground" />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// --- Pipeline DAG Tab ---

const STATUS_COLORS: Record<string, { bg: string; border: string; text: string }> = {
  pending:   { bg: "#1a1a2e", border: "#3b3b5c", text: "#9ca3af" },
  assigned:  { bg: "#1a1a2e", border: "#6366f1", text: "#a5b4fc" },
  running:   { bg: "#0c1a3a", border: "#3b82f6", text: "#93c5fd" },
  executing: { bg: "#0c1a3a", border: "#3b82f6", text: "#93c5fd" },
  dispatched:{ bg: "#0c1a3a", border: "#3b82f6", text: "#93c5fd" },
  completed: { bg: "#052e16", border: "#22c55e", text: "#86efac" },
  verified:  { bg: "#052e16", border: "#22c55e", text: "#86efac" },
  failed:    { bg: "#2a0a0a", border: "#ef4444", text: "#fca5a5" },
  cancelled: { bg: "#1a1a2e", border: "#6b7280", text: "#9ca3af" },
};

function statusColor(status: string) {
  return STATUS_COLORS[status] ?? STATUS_COLORS.pending;
}

function PipelineTab({ task }: { task: TaskDetail }) {
  const steps = task.steps || [];

  const { nodes, edges } = useMemo(() => {
    const ns: Node[] = [];
    const es: Edge[] = [];
    const nodeW = 200;
    const nodeH = 60;
    const gapY = 90;
    const centerX = 300;

    const managerColor = statusColor("completed");
    ns.push({
      id: "manager",
      position: { x: centerX - nodeW / 2, y: 0 },
      data: { label: "Manager" },
      style: {
        width: nodeW, height: nodeH,
        background: managerColor.bg, border: `2px solid ${managerColor.border}`,
        color: managerColor.text, borderRadius: 12,
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 13, fontWeight: 600,
      },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
    });

    const plannerStatus = task.status === "completed" || task.status === "failed" ? task.status : "running";
    const plannerColor = statusColor(plannerStatus);
    ns.push({
      id: "planner",
      position: { x: centerX - nodeW / 2, y: gapY },
      data: { label: <span title={task.planner_id || "Planner"}>{`Planner ${task.planner_id ? `(${task.planner_id.slice(0, 20)})` : ""}`}</span> },
      style: {
        width: nodeW, height: nodeH,
        background: plannerColor.bg, border: `2px solid ${plannerColor.border}`,
        color: plannerColor.text, borderRadius: 12,
        display: "flex", alignItems: "center", justifyContent: "center",
        fontSize: 13, fontWeight: 600,
      },
      sourcePosition: Position.Bottom,
      targetPosition: Position.Top,
    });
    es.push({ id: "e-manager-planner", source: "manager", target: "planner", animated: plannerStatus === "running" });

    if (steps.length === 0) {
      const waitColor = statusColor("pending");
      ns.push({
        id: "waiting",
        position: { x: centerX - nodeW / 2, y: gapY * 2 },
        data: { label: "Waiting for steps..." },
        style: {
          width: nodeW, height: nodeH,
          background: waitColor.bg, border: `2px dashed ${waitColor.border}`,
          color: waitColor.text, borderRadius: 12,
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 12, fontStyle: "italic",
        },
        sourcePosition: Position.Bottom,
        targetPosition: Position.Top,
      });
      es.push({ id: "e-planner-wait", source: "planner", target: "waiting", animated: true });
    } else {
      const totalWidth = steps.length * (nodeW + 40) - 40;
      const startX = centerX - totalWidth / 2;

      steps.forEach((step, i) => {
        const stepId = `step-${i}`;
        const sc = statusColor(step.status);
        const x = startX + i * (nodeW + 40);
        const y = gapY * 2;

        ns.push({
          id: stepId,
          position: { x, y },
          data: {
            label: (
              <span title={step.instruction} className="block h-full w-full overflow-hidden">
                {`Step ${i + 1}: ${step.instruction.slice(0, 30)}${step.instruction.length > 30 ? "…" : ""}`}
              </span>
            ),
          },
          style: {
            width: nodeW, height: nodeH,
            background: sc.bg, border: `2px solid ${sc.border}`,
            color: sc.text, borderRadius: 12,
            display: "flex", alignItems: "center", justifyContent: "center",
            fontSize: 11, fontWeight: 500,
            padding: "0 8px", textAlign: "center" as const,
          },
          sourcePosition: Position.Bottom,
          targetPosition: Position.Top,
        });
        es.push({
          id: `e-planner-${stepId}`,
          source: "planner",
          target: stepId,
          animated: step.status === "dispatched" || step.status === "running",
        });

        if (step.worker_id) {
          const workerId = `worker-${i}`;
          const wc = statusColor(step.status === "verified" || step.status === "completed" ? "completed" : step.status);
          ns.push({
            id: workerId,
            position: { x, y: gapY * 3 },
            data: { label: <span title={step.worker_id}>{step.worker_id.slice(0, 20)}</span> },
            style: {
              width: nodeW, height: 44,
              background: wc.bg, border: `1px solid ${wc.border}`,
              color: wc.text, borderRadius: 8,
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 10,
            },
            sourcePosition: Position.Bottom,
            targetPosition: Position.Top,
          });
          es.push({
            id: `e-${stepId}-${workerId}`,
            source: stepId,
            target: workerId,
            animated: step.status === "dispatched",
          });
        }
      });

      const resultY = gapY * (steps.some(s => s.worker_id) ? 4 : 3);
      const resultStatus = task.status === "completed" ? "completed" : task.status === "failed" ? "failed" : "pending";
      const rc = statusColor(resultStatus);
      ns.push({
        id: "result",
        position: { x: centerX - nodeW / 2, y: resultY },
        data: { label: task.status === "completed" ? "Task Complete" : task.status === "failed" ? "Task Failed" : "In Progress..." },
        style: {
          width: nodeW, height: nodeH,
          background: rc.bg, border: `2px solid ${rc.border}`,
          color: rc.text, borderRadius: 12,
          display: "flex", alignItems: "center", justifyContent: "center",
          fontSize: 13, fontWeight: 600,
        },
        targetPosition: Position.Top,
      });

      steps.forEach((step, i) => {
        const sourceId = step.worker_id ? `worker-${i}` : `step-${i}`;
        es.push({
          id: `e-${sourceId}-result`,
          source: sourceId,
          target: "result",
        });
      });
    }

    return { nodes: ns, edges: es };
  }, [task, steps]);

  return (
    <div className="h-[500px] w-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        defaultEdgeOptions={{
          type: "smoothstep",
          style: { stroke: "#4b5563", strokeWidth: 2 },
        }}
      >
        <Background color="#1f2937" gap={20} />
      </ReactFlow>
    </div>
  );
}

// --- Project ---

/** The task's project, with a select to move it to another one (any status). */
function TaskProjectControl({
  task,
  onMoved,
}: {
  task: TaskDetail;
  onMoved: (projectId: string, projectName: string) => void;
}) {
  const { projects } = useProjects();
  // The project picked but not saved yet; null shows the task's own.
  const [picked, setPicked] = useState<string | null>(null);
  const target = picked ?? task.project_id;
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [moved, setMoved] = useState(false);

  useEffect(() => {
    if (!moved) return;
    const timer = setTimeout(() => setMoved(false), 3000);
    return () => clearTimeout(timer);
  }, [moved]);

  const move = async () => {
    setSaving(true);
    setError("");
    try {
      const result = await setTaskProject(task.task_id, target);
      onMoved(result.project_id, result.project_name);
      setPicked(null);
      setMoved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const options = projects ?? [];
  const listed = options.some((p) => p.project_id === task.project_id);

  return (
    <span className="flex items-center gap-1.5">
      <Folder className="h-3.5 w-3.5 text-muted-foreground" />
      <select
        aria-label="Project"
        value={target}
        onChange={(e) => {
          setPicked(e.target.value === task.project_id ? null : e.target.value);
          setError("");
          setMoved(false);
        }}
        className="h-6 max-w-[14rem] rounded-md border border-input bg-transparent px-1.5 text-xs text-foreground"
      >
        {!listed && <option value={task.project_id}>{task.project_name || task.project_id}</option>}
        {options.map((p) => (
          <option key={p.project_id} value={p.project_id}>{p.name}</option>
        ))}
      </select>
      {target !== task.project_id && (
        <>
          <button
            type="button"
            onClick={move}
            disabled={saving}
            className="flex items-center gap-1 rounded-md bg-primary px-2 py-0.5 text-xs text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-50"
          >
            {saving && <Loader2 className="h-3 w-3 animate-spin" />}
            Move
          </button>
          <button
            type="button"
            onClick={() => {
              setPicked(null);
              setError("");
            }}
            className="rounded-md px-1.5 py-0.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            Cancel
          </button>
        </>
      )}
      {moved && (
        <span role="status" className="flex items-center gap-1 text-xs text-success">
          <CheckCircle2 className="h-3 w-3" />
          Moved to {task.project_name}
        </span>
      )}
      {error && <span role="alert" className="text-xs text-destructive">{error}</span>}
    </span>
  );
}

// --- Main Page ---

interface TaskDetailPageProps {
  taskId: string;
  onBack: () => void;
}

export function TaskDetailPage({ taskId, onBack }: TaskDetailPageProps) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [error, setError] = useState("");
  const [startError, setStartError] = useState("");
  const [tab, setTab] = useState<Tab>("overview");
  // Bumped when the task runs again, so the page polls it again.
  const [runs, setRuns] = useState(0);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval>;
    let stopped = false;
    const load = () =>
      fetchTaskDetail(taskId)
        .then((t) => {
          if (stopped) return;
          setTask(t);
          setError("");
          if (["completed", "failed", "cancelled", "stopped", "init"].includes(t.status)) {
            clearInterval(timer);
          }
        })
        .catch((e) => { if (!stopped) setError(e.message); });
    load();
    timer = setInterval(load, 2000);
    return () => { stopped = true; clearInterval(timer); };
  }, [taskId, runs]);

  const tabs: { id: Tab; label: string; icon: React.ElementType }[] = [
    { id: "overview", label: "Overview", icon: Activity },
    { id: "pipeline", label: "Pipeline", icon: GitBranch },
    { id: "sessions", label: "Sessions", icon: Terminal },
    { id: "conversation", label: "Conversation", icon: MessageSquare },
  ];

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="border-b border-border px-5 py-3">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onBack}
            aria-label="Back to tasks"
            title="Back to tasks"
            className="flex h-8 w-8 items-center justify-center rounded-md hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          </button>
          <div className="min-w-0 flex-1">
            <h1 className="text-lg font-semibold leading-tight truncate">
              {task?.name || task?.requirement || taskId}
            </h1>
            <div className="flex items-center gap-2 mt-0.5 flex-wrap">
              <span className="text-xs text-muted-foreground">{taskId}</span>
              {task && taskStatusBadge(task.status)}
              {task && <ScheduledBadge task={task} />}
              {task && task.phase && task.phase !== task.status && phaseBadge(task.phase)}
              {task && (
                <TaskProjectControl
                  task={task}
                  onMoved={(projectId, projectName) =>
                    setTask((t) => (t ? { ...t, project_id: projectId, project_name: projectName } : t))
                  }
                />
              )}
              {task && Object.keys(task.profile_overrides ?? {}).length > 0 && (
                <span className="text-xs text-muted-foreground">
                  {AGENT_PROFILE.plural}:{" "}
                  {(["planner", "worker", "reviewer"] as const)
                    .filter((role) => task.profile_overrides?.[role])
                    .map((role) => `${role} → ${task.profile_overrides?.[role]?.name}`)
                    .join(", ")}
                </span>
              )}
            </div>
          </div>
          {task && isActiveTask(task) && (
            <button
              type="button"
              onClick={() => stopTask(taskId).then(() => {})}
              className="flex items-center gap-1.5 rounded-md border border-warning/50 px-3 py-1.5 text-xs font-medium text-warning hover:bg-warning/10 transition-colors"
            >
              <Pause className="h-3.5 w-3.5" />
              Stop
            </button>
          )}
          {task && ["stopped", "failed", "cancelled", "init"].includes(task.status) && (
            <button
              type="button"
              onClick={() => (task.status === READY_STATUS ? startReadyTask : startTask)(taskId).then(() => { setStartError(""); setRuns((n) => n + 1); }, (err) => setStartError(err instanceof Error ? err.message : String(err)))}
              className="flex items-center gap-1.5 rounded-md border border-success/50 px-3 py-1.5 text-xs font-medium text-success hover:bg-success/10 transition-colors"
            >
              <Play className="h-3.5 w-3.5" />
              Start
            </button>
          )}
        </div>
        {startError && <p className="mt-1 text-xs text-destructive">{startError}</p>}

        {/* Tabs */}
        <div className="flex gap-1 mt-3">
          {tabs.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className={cn(
                "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                tab === id
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-accent hover:text-foreground",
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {error && !task && (
          <div className="flex h-full items-center justify-center">
            <p className="text-sm text-destructive">{error}</p>
          </div>
        )}

        {!task && !error && (
          <div className="flex h-full items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
          </div>
        )}

        {task && tab === "overview" && (
          <OverviewTab
            task={task}
            onContinued={() => setRuns((n) => n + 1)}
            onConcurrency={(concurrency) => setTask((t) => (t ? { ...t, concurrency } : t))}
          />
        )}
        {task && tab === "pipeline" && <PipelineTab task={task} />}
        {task && tab === "sessions" && <SessionsTab taskId={taskId} />}
        {task && tab === "conversation" && <ConversationTab taskId={taskId} />}
      </div>
    </div>
  );
}
