import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  ChevronDown,
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
  RotateCcw,
  CirclePause,
  MessageCircleQuestionMark,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ClampedText } from "@/components/clamped-text";
import { Markdown } from "@/components/markdown";
import { ScheduledBadge } from "@/components/scheduled-badge";
import { historyHref, runRoleLabel } from "@/pages/agent-history";
import { TaskCostBreakdown } from "@/components/task-cost";
import { ContinueAtCap, TaskSpendMeter } from "@/components/task-budget";
import { TaskRoleEditor } from "@/components/role-editor";
import { RoleProfile, TaskAgentsSummary, TaskAgentsTab } from "@/components/task-agents-tab";
import { NodeCards } from "@/components/pipeline-node-card";
import { PipelineToolbar } from "@/components/pipeline-toolbar";
import { GateBadge, TaskGates } from "@/components/task-gates";
import { GATE_STATES, GATE_TURNS, gateAnchor, gateLook, gateTurn, stepAnchor } from "@/lib/gates";
import { type NodeCard, gateCard, nodeCardId, plannerCard, resultCard, stepCard } from "@/lib/node-cards";
import { FIT_PADDING, shownSteps, stepColumns, stepLevels, storeHideSkipped, storedHideSkipped } from "@/lib/pipeline-layout";
import { useFullScreen } from "@/lib/full-screen";
import { costBasis, costCaveats, formatUsd } from "@/lib/usage";
import { cn } from "@/lib/utils";
import {
  ReactFlow,
  Background,
  type Node,
  type NodeProps,
  type Edge,
  Controls,
  Handle,
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
  type TaskDetail,
  type Supplement,
  type ManagerAction,
  type TaskStep,
  type PlannerRole,
  type RoleStats,
  type JournalEntry,
  type ProgressNote,
  type ActivityEvent,
  type GateState,
  type UserGate,
  type RunRole,
} from "@/lib/api";

type Tab = "overview" | "pipeline" | "agents" | "sessions" | "conversation";

function stepStatusIcon(status: string) {
  switch (status) {
    case "dispatched":
    case "running":
      return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
    case "verified":
      return <CheckCircle2 className="h-4 w-4 text-success" />;
    case "completed":
      return <Clock className="h-4 w-4 text-primary" />;
    case "deciding":
      return <Clock className="h-4 w-4 text-warning" />;
    case "waiting_user":
      return <MessageCircleQuestionMark className="h-4 w-4 text-warning" />;
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
      // The Worker finished; the step is done only once it is accepted (verified).
      return <Badge variant="default">In review</Badge>;
    case "deciding":
      return <Badge variant="warning">Awaiting decision</Badge>;
    case "waiting_user":
      return <Badge variant="warning">Waiting for you</Badge>;
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
    case "waiting_user":
      return <Badge variant="warning" className="text-sm">Waiting for you</Badge>;
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
  if (via === "gate") return "gate conclusion";
  if (via.startsWith("manager:")) return `Manager (${via.slice("manager:".length)})`;
  return via || "—";
}

/** A started task's requirement: the current text, its earlier versions, and an edit its Planner gets in a turn. */
function TaskRequirement({ task }: { task: TaskDetail }) {
  const versions = task.requirement_versions ?? [];
  // Starting or running, whatever its Planner is doing; one not started edits it above.
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

/** Take the user to a gate's place on the Overview: its panel or closed line (opened), else its row in Steps. */
function showGate(step: TaskStep) {
  const place = document.getElementById(gateAnchor(step.step_key)) ?? document.getElementById(stepAnchor(step.id));
  if (!place) return;
  if (place instanceof HTMLDetailsElement) place.open = true;
  place.scrollIntoView({ block: "start" });
  const target = place instanceof HTMLDetailsElement ? place.querySelector("summary") : place;
  target?.focus({ preventScroll: true });
}

function OverviewTab({
  task,
  onContinued,
  onOpenAgents,
  onRoleEdited,
  onGateChanged,
  gateFocus,
  onGateShown,
}: {
  task: TaskDetail;
  onContinued: () => void;
  onOpenAgents: () => void;
  /** After the user saved a new version of one of the task's roles. */
  onRoleEdited: () => void;
  /** After the user wrote in a gate or ended its discussion, with the gate as the server answered. */
  onGateChanged: (gate: UserGate) => void;
  /** The gate step the Pipeline asked to show, until it is shown. */
  gateFocus: TaskStep | null;
  onGateShown: () => void;
}) {
  useEffect(() => {
    if (!gateFocus) return;
    showGate(gateFocus);
    onGateShown();
  }, [gateFocus, onGateShown]);
  const steps = task.steps || [];
  // A gate step shows its gate's state, or whose turn it is while it is discussed.
  const gates = new Map((task.gates ?? []).map((gate) => [gate.step_id, gate]));
  const gateOf = (step: TaskStep) => (step.kind === "gate" ? gates.get(step.id) : undefined);
  const stepIcon = (step: TaskStep, gate = gateOf(step)) =>
    gate ? <CirclePause className={cn("h-4 w-4", gateLook(gate, isActive).tone)} aria-hidden="true" /> : stepStatusIcon(step.status);
  const stepBadge = (step: TaskStep, gate = gateOf(step)) =>
    gate ? <GateBadge gate={gate} active={isActive} /> : stepStatusBadge(step.status);
  // Done means accepted (verified); a step in review is not done yet. A dropped (skipped) step is not counted, nor is
  // a cancelled gate (its step is verified, but nothing was settled).
  const cancelledGate = (s: TaskStep) => gateOf(s)?.state === "cancelled";
  const planned = steps.filter((s) => s.status !== "skipped" && !cancelledGate(s));
  const done = planned.filter((s) => s.status === "verified").length;
  const inReview = planned.filter((s) => s.status === "completed").length;
  const dropped = steps.filter((s) => s.status === "skipped").length;
  const cancelledGates = steps.filter(cancelledGate).length;
  const isActive = isActiveTask(task);

  return (
    <div className="space-y-6 p-5">
      <TaskGates task={task} onChanged={onGateChanged} />
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
            {steps.length > 0 ? `${done}/${planned.length} done` : "No steps yet"}
          </p>
          {(inReview > 0 || dropped > 0 || cancelledGates > 0) && (
            <p className="mt-0.5 text-xs text-muted-foreground">
              {[
                inReview > 0 && `${inReview} in review`,
                dropped > 0 && `${dropped} skipped`,
                cancelledGates > 0 && `${cancelledGates} gate${cancelledGates === 1 ? "" : "s"} cancelled`,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          )}
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

      {task.agents && <TaskAgentsSummary agents={task.agents} onOpen={onOpenAgents} />}

      <TaskRoles task={task} onEdited={onRoleEdited} />

      {steps.length > 0 && (
        <div>
          <h3 className="text-sm font-semibold mb-2">Steps</h3>
          <div className="rounded-lg border border-border bg-card divide-y divide-border">
            {steps.map((step: TaskStep) => (
              <div key={step.id} id={stepAnchor(step.id)} tabIndex={-1} className="px-4 py-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
                <div className="flex items-start gap-3">
                  {stepIcon(step)}
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="text-xs text-muted-foreground">#{step.step_index + 1}</span>
                      {step.kind === "gate" && <span className="font-mono text-xs text-muted-foreground">gate {step.step_key}</span>}
                      {stepBadge(step)}
                      {step.worker_id && (
                        <a
                          href={historyHref(step.worker_id, task.task_id)}
                          className="text-xs text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground"
                          title="打开该 Worker 的运行详情（启动命令、输入 prompt、stream-json 事件）"
                        >
                          {step.worker_id}
                        </a>
                      )}
                      {stepRoles(step) && <span className="font-mono text-xs text-muted-foreground">{stepRoles(step)}</span>}
                      {step.status === "completed" && step.review_stage && (
                        <span className="text-xs text-muted-foreground">
                          Waiting for{" "}
                          {step.review_stage === "role" ? `review: ${step.review_role_key}` : "the built-in review"}
                        </span>
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

      <AgentTimeline taskId={task.task_id} runRoles={task.run_roles} />

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

// --- Roles: the roles the task's Planner defined for its steps ---

/** The roles a step runs and is reviewed as, e.g. "as tester v2 + review: security (advisory)"; "" for the
 * built-in ones. */
function stepRoles(step: TaskStep, versions = true) {
  const version = (v: number | null) => (versions && v != null ? ` v${v}` : "");
  const advisory = step.review_role_required === false ? " (advisory)" : "";
  return [
    step.role_key && `as ${step.role_key}${version(step.role_version)}`,
    step.review_role_key && `+ review: ${step.review_role_key}${version(step.review_role_version)}${advisory}`,
  ].filter(Boolean).join(" ");
}

const percent = (rate: number | null) => (rate == null ? "—" : `${Math.round(rate * 100)}%`);

/** A role's numbers on one wrapping line, the same for the built-in Worker and Reviewer, so they compare. */
function RoleStatsLine({ stats }: { stats: RoleStats }) {
  const reviews = stats.reviews.passed + stats.reviews.not_passed;
  const items: [string, string][] = [
    ["Runs", stats.running ? `${stats.runs} (${stats.running} running)` : String(stats.runs)],
    ["Cost", formatUsd(stats.cost_usd)],
    ["Steps", `${stats.accepted}/${stats.steps} accepted, ${stats.failed} failed`],
    ["Retries", String(stats.retries)],
    ["Contract violations", String(stats.contract_violations)],
    ["Review pass rate", reviews ? `${percent(stats.review_pass_rate)} of ${reviews}` : "—"],
  ];
  return (
    <span className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs">
      {items.map(([label, value]) => (
        <span key={label}>
          <span className="text-muted-foreground">{label}</span> <span className="font-mono tabular-nums">{value}</span>
        </span>
      ))}
    </span>
  );
}

const ROLE_TEXT = "mt-1 max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-md bg-muted px-3 py-2 font-mono text-xs";

/** Who wrote a version, and the template it was copied from. */
function versionSource(v: { via: PlannerRole["via"]; template: string | null }) {
  return [v.via === "user" && "by you", v.template && `from template ${v.template}`].filter(Boolean).join(", ");
}

/** A role's line (kind, version, profile, the steps that name it, its numbers), opening on its prompt, harness and
 * versions, and, while the task has not completed, the user's edit. */
function RoleRow({ role, task, onEdited }: { role: PlannerRole; task: TaskDetail; onEdited: () => void }) {
  return (
    <li>
      <details className="group">
        <summary className="flex min-h-11 cursor-pointer list-none items-start gap-2 px-4 py-3 hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
          <ChevronDown
            aria-hidden="true"
            className="mt-0.5 h-4 w-4 shrink-0 -rotate-90 text-muted-foreground transition-transform duration-150 group-open:rotate-0 motion-reduce:transition-none"
          />
          <span className="min-w-0 flex-1 space-y-1">
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span className="break-all font-mono text-sm font-semibold">{role.id}</span>
              <Badge variant="outline">{role.kind === "work" ? "Work" : "Review"}</Badge>
              <span className="font-mono text-xs text-muted-foreground">v{role.version}</span>
              {role.via === "user" && (
                <Badge variant="default" title="You wrote its current version: the Planner cannot change it">
                  edited by you
                </Badge>
              )}
              <RoleProfile role={role} />
            </span>
            <span className="block text-xs">
              <span className="text-muted-foreground">Steps</span>{" "}
              {role.steps.length ? <span className="font-mono">{role.steps.join(", ")}</span> : "none yet"}
            </span>
            <RoleStatsLine stats={role.stats} />
          </span>
        </summary>
        <div className="space-y-3 border-t border-border py-3 pl-10 pr-4 text-xs">
          {task.agents?.editable && (
            <TaskRoleEditor taskId={task.task_id} projectId={task.project_id ?? null} role={role} onSaved={onEdited} />
          )}
          <div>
            <p className="font-medium text-muted-foreground">Role prompt</p>
            <pre className={ROLE_TEXT}>{role.role_prompt}</pre>
          </div>
          <div>
            <p className="font-medium text-muted-foreground">Harness</p>
            {role.harness ? (
              <pre className={ROLE_TEXT}>{role.harness}</pre>
            ) : (
              <p className="mt-1">None of its own: the working style that applies.</p>
            )}
          </div>
          <div>
            <p className="font-medium text-muted-foreground">Versions and reasons</p>
            <ol className="mt-1 space-y-1">
              {[...role.versions].reverse().map((v) => (
                <li key={v.version} className="whitespace-pre-wrap break-words">
                  <span className="font-mono">v{v.version}</span>
                  <span className="text-muted-foreground"> · {formatTime(v.created_at)}</span>
                  {versionSource(v) && <span className="text-muted-foreground"> · {versionSource(v)}</span>}
                  {v.reason && ` · ${v.reason}`}
                </li>
              ))}
            </ol>
          </div>
        </div>
      </details>
    </li>
  );
}

/** The roles the task's Planner defined, then the built-in Worker's and Reviewer's numbers to compare them with;
 * nothing while it defined none. */
function TaskRoles({ task, onEdited }: { task: TaskDetail; onEdited: () => void }) {
  if (!task.roles?.length) return null;
  const builtin = task.builtin_role_stats;
  const builtinRows: [string, string, RoleStats][] = builtin
    ? [
        ["Built-in Worker", "the steps no work role runs", builtin.worker],
        ["Built-in Reviewer", "every reviewed step", builtin.reviewer],
      ]
    : [];
  return (
    <section aria-labelledby="task-roles-title">
      <h3 id="task-roles-title" className="mb-2 text-sm font-semibold">
        Roles
      </h3>
      <ul className="divide-y divide-border rounded-lg border border-border bg-card">
        {task.roles.map((role) => (
          <RoleRow key={role.id} role={role} task={task} onEdited={onEdited} />
        ))}
        {builtinRows.map(([name, detail, stats]) => (
          <li key={name} className="space-y-1 py-3 pl-10 pr-4">
            <p className="text-xs">
              <span className="text-sm font-semibold">{name}</span>
              <span className="text-muted-foreground"> · for comparison: {detail}</span>
            </p>
            <RoleStatsLine stats={stats} />
          </li>
        ))}
      </ul>
    </section>
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
  profile_choice: { label: "Profile choice", variant: "outline" },
  concurrency_change: { label: "Concurrency", variant: "outline" },
  role_defined: { label: "Role", variant: "outline" },
  gate_opened: { label: "Gate opened", variant: "warning" },
  gate_updated: { label: "Gate updated", variant: "outline" },
  gate_cancelled: { label: "Gate cancelled", variant: "outline" },
  gate_closed: { label: "Gate passed", variant: "success" },
};

/** The task journal, newest first: steps accepted, failed or retried with what their Worker and Reviewer
 * learned, the Planner's decisions, its choices of profile and concurrency and the roles it defines, the
 * supplements, and new Planner sessions with their handoffs. */
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

/** Each agent's events; a run as a role its task's Planner defined is named by it (`runRoles`, by agent id). */
function AgentTimeline({ taskId, runRoles }: { taskId: string; runRoles?: Record<string, RunRole> }) {
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
    const runRole = runRoles?.[name];
    if (runRole) return runRoleLabel(runRole);
    if (name.startsWith("planner")) return "Planner";
    if (name.startsWith("worker")) return "Worker";
    if (name.startsWith("reviewer")) return "Reviewer";
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
              <div className="flex flex-col sm:flex-row sm:items-start">
              <button
                type="button"
                onClick={() => toggle(agent)}
                onDoubleClick={() => { window.location.hash = historyHref(agent, taskId); }}
                title="单击展开/折叠，双击打开该 agent 的运行详情（启动命令、输入 prompt、stream-json 事件）"
                className="min-w-0 flex-1 text-left px-4 py-3 hover:brightness-110 transition-all"
              >
                <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
                  <div className="flex min-w-0 items-center gap-2">
                    <span
                      className="font-mono text-xs tabular-nums opacity-70"
                      title={formatTime(first.created_at)}
                    >
                      {formatClock(first.created_at)}
                    </span>
                    <Bot className="h-4 w-4 shrink-0" />
                    <span className="min-w-0 break-all text-sm font-medium">{agent}</span>
                    <Badge variant="outline" className={cn("text-[10px] py-0 h-4 shrink-0", runRoles?.[agent] && "font-mono")}>
                      {agentRole(agent)}
                    </Badge>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs opacity-70">
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
                className="mx-4 mb-3 flex shrink-0 items-center gap-1 self-start rounded-md border border-current/30 px-2 py-1 text-xs opacity-80 hover:opacity-100 hover:bg-white/5 sm:m-2.5"
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

/** How a node of the pipeline shows a status: a word (color is never the only signal) and a look built from the
 * theme's tokens. */
interface NodeLook {
  label: string;
  border: string;
  borderStyle: "solid" | "dashed";
  background: string;
  color: string;
  muted?: boolean;
}

const tint = (token: string, percent: number) => `color-mix(in srgb, var(${token}) ${percent}%, var(--color-card))`;

/** One look per step status (task_steps.status); "completed" is a step awaiting its review. */
const STEP_LOOKS: Record<string, NodeLook> = {
  pending: { label: "Not started", border: "var(--color-border)", borderStyle: "solid", background: "var(--color-card)",
             color: "var(--color-muted-foreground)" },
  dispatched: { label: "Running", border: "var(--color-primary)", borderStyle: "solid",
                background: tint("--color-primary", 16), color: "var(--color-foreground)" },
  deciding: { label: "Awaiting decision", border: "var(--color-warning)", borderStyle: "solid",
              background: tint("--color-warning", 12), color: "var(--color-foreground)" },
  completed: { label: "In review", border: "var(--color-primary)", borderStyle: "dashed",
               background: tint("--color-primary", 8), color: "var(--color-foreground)" },
  verified: { label: "Done", border: "var(--color-success)", borderStyle: "solid",
              background: tint("--color-success", 12), color: "var(--color-foreground)" },
  failed: { label: "Failed", border: "var(--color-destructive)", borderStyle: "solid",
            background: tint("--color-destructive", 12), color: "var(--color-foreground)" },
  skipped: { label: "Skipped", border: "var(--color-muted-foreground)", borderStyle: "dashed",
             background: "var(--color-background)", color: "var(--color-muted-foreground)", muted: true },
};

function stepLook(status: string): NodeLook {
  return STEP_LOOKS[status] ?? { ...STEP_LOOKS.pending, label: status };
}

/** One look per gate state, in the steps' tokens; the gate's shape (a hexagon with a pause icon) sets it apart. */
const GATE_LOOKS: Record<GateState, NodeLook> = {
  pending: { ...STEP_LOOKS.pending, label: GATE_STATES.pending.label },
  open: { label: GATE_STATES.open.label, border: "var(--color-warning)", borderStyle: "solid",
          background: tint("--color-warning", 14), color: "var(--color-foreground)" },
  discussing: { ...STEP_LOOKS.dispatched, label: GATE_STATES.discussing.label },
  passed: { ...STEP_LOOKS.verified, label: GATE_STATES.passed.label },
  cancelled: { label: GATE_STATES.cancelled.label, border: "var(--color-muted-foreground)", borderStyle: "dashed",
               background: "var(--color-background)", color: "var(--color-muted-foreground)" },
  skipped: { ...STEP_LOOKS.skipped, label: GATE_STATES.skipped.label },
};

/** A discussing gate's look by whose turn it is (`gateTurn`): the user's like an open gate, the Planner's like a
 * running step. */
const GATE_TURN_LOOKS: Record<keyof typeof GATE_TURNS, NodeLook> = {
  user: { ...GATE_LOOKS.open, label: GATE_TURNS.user.label },
  planner: { ...STEP_LOOKS.dispatched, label: GATE_TURNS.planner.label },
};

/** A gate step's state: its gate's, else what its step status says. */
function gateState(step: TaskStep, gate: UserGate | undefined): GateState {
  if (gate) return gate.state;
  if (step.status === "waiting_user") return "open";
  if (step.status === "verified") return "passed";
  return step.status === "skipped" ? "skipped" : "pending";
}

/** The Planner's and the result node's look, from the task's status. */
function taskLook(status: string, live: string): NodeLook {
  if (status === "completed") return { ...STEP_LOOKS.verified, label: "Completed" };
  if (status === "failed") return { ...STEP_LOOKS.failed, label: "Failed" };
  const quiet: Record<string, string> = { stopped: "Stopped", cancelled: "Cancelled", pending: "Starting",
                                         [READY_STATUS]: READY_LABEL };
  return status in quiet ? { ...STEP_LOOKS.pending, label: quiet[status] } : { ...STEP_LOOKS.dispatched, label: live };
}

/** Why each dropped step was dropped, from the journal: the decision that dropped it (a replan names the steps it
 * dropped or stopped; a decided step that a replan dropped has an entry of its own). */
function droppedBecause(journal: JournalEntry[], steps: TaskStep[]): Map<string, string> {
  const byKey = new Map(steps.filter((s) => s.status === "skipped" && s.step_key).map((s) => [s.step_key, s.id]));
  const why = new Map<string, string>();
  for (const entry of journal) {
    if (entry.kind !== "decision") continue;
    if (entry.step_id && entry.summary.includes("The step was dropped")) why.set(entry.step_id, entry.summary);
    for (const match of entry.summary.matchAll(/(?:dropped|stopped) ([\w.-]+(?:, [\w.-]+)*)/g)) {
      for (const name of match[1].split(", ")) {
        const id = byKey.get(name.replace(/\.$/, ""));
        if (id && !why.has(id)) why.set(id, entry.summary);
      }
    }
  }
  return why;
}

// xyflow's dark mode, in the theme's tokens.
const PIPELINE_THEME = {
  "--xy-background-color": "var(--color-background)",
  "--xy-controls-button-background-color": "var(--color-muted)",
  "--xy-controls-button-background-color-hover": "var(--color-accent)",
  "--xy-controls-button-color": "var(--color-foreground)",
  "--xy-controls-button-border-color": "var(--color-border)",
} as CSSProperties;

const NODE_W = 180;
const NODE_H = 60;
const GAP_X = 36;
const ROW_H = 110;

function nodeStyle(look: NodeLook) {
  return {
    width: NODE_W, height: NODE_H, padding: 0, borderRadius: 12,
    background: look.background, color: look.color,
    border: `2px ${look.borderStyle} ${look.border}`,
    opacity: look.muted ? 0.8 : 1,
    // React Flow leaves a node that is neither selectable nor draggable without pointer events; its card needs them.
    pointerEvents: "all" as const,
  };
}

/** Nodes that name their card (`NodeCards`) for assistive technology, where focus lands: on the node, or on a gate's
 * button (`GateNode`). */
const described = (nodes: Node[]): Node[] =>
  nodes.map((node) => (node.type === "gate" ? node : { ...node, domAttributes: { "aria-details": nodeCardId(node.id) } }));

function NodeLabel({
  name,
  look,
  note,
  role,
  detail,
}: {
  name: string;
  look: NodeLook;
  note?: string;
  /** The roles a step runs and is reviewed as. */
  role?: string;
  /** A line of what the step does, shown when there is no role line (the node fits one of the two). */
  detail?: string;
}) {
  return (
    <div className="flex h-full w-full flex-col items-center justify-center overflow-hidden px-2 leading-tight">
      <span className={cn("max-w-full truncate font-mono text-xs font-semibold", look.muted && "line-through")}>{name}</span>
      {role ? (
        <span className="mt-0.5 max-w-full truncate font-mono text-[10px]">{role}</span>
      ) : (
        detail && <span className="mt-0.5 max-w-full truncate text-[10px] opacity-80">{detail}</span>
      )}
      <span className="mt-1 max-w-full truncate text-[10px]">{look.label}{note ? ` · ${note}` : ""}</span>
    </div>
  );
}

interface GateNodeData extends Record<string, unknown> {
  name: string;
  topic: string;
  look: NodeLook;
  onOpen: () => void;
}

const HEXAGON_INSET = 18;
const HEXAGON = [
  [HEXAGON_INSET, 1], [NODE_W - HEXAGON_INSET, 1], [NODE_W - 1, NODE_H / 2],
  [NODE_W - HEXAGON_INSET, NODE_H - 1], [HEXAGON_INSET, NODE_H - 1], [1, NODE_H / 2],
].map((point) => point.join(",")).join(" ");

/** A user gate in the pipeline: a hexagon with a pause icon, its label and its state in words; a button that takes
 * the user to the gate on the Overview tab. */
function GateNode({ id, data }: NodeProps<Node<GateNodeData, "gate">>) {
  const { look } = data;
  return (
    <>
      <Handle type="target" position={Position.Top} />
      <svg width={NODE_W} height={NODE_H} className="absolute inset-0" aria-hidden="true">
        <polygon
          points={HEXAGON}
          fill={look.background}
          stroke={look.border}
          strokeWidth={2}
          strokeDasharray={look.borderStyle === "dashed" ? "6 4" : undefined}
        />
      </svg>
      <button
        type="button"
        onClick={data.onOpen}
        aria-details={nodeCardId(id)}
        style={{ color: look.color, opacity: look.muted ? 0.8 : 1 }}
        className="nodrag nopan pointer-events-auto relative flex h-full w-full cursor-pointer flex-col items-center justify-center rounded-md px-6 leading-tight focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span className={cn("flex max-w-full items-center gap-1 font-mono text-xs font-semibold", look.muted && "line-through")}>
          <CirclePause className="h-3 w-3 shrink-0" aria-hidden="true" />
          <span className="truncate">{data.name}</span>
        </span>
        <span className="mt-1 max-w-full truncate text-[10px]">{look.label}</span>
        <span className="sr-only">: {data.topic}. Show this gate on the Overview tab</span>
      </button>
      <Handle type="source" position={Position.Bottom} />
    </>
  );
}

const NODE_TYPES = { gate: GateNode };

/** The plan as its dependency graph: each step a node under the steps it depends on (steps without dependencies
 * under the Planner), the steps nothing live depends on leading to the result; a user gate is a hexagon that opens
 * it on the Overview tab (`onOpenGate`). Done steps keep their place and edges when the plan changes; dropped ones
 * stay, dashed and muted, as history. Each node has a card of its details (`NodeCards`); the dropped steps are
 * hidden unless the viewer shows them, and the graph can go full screen (`PipelineToolbar`). */
function PipelineTab({ task, onOpenGate }: { task: TaskDetail; onOpenGate: (step: TaskStep) => void }) {
  const [hideSkipped, setHideSkipped] = useState(storedHideSkipped);
  const container = useRef<HTMLDivElement>(null);
  const [full, toggleFull] = useFullScreen(container);
  const { nodes, edges, rows, cards, skipped } = useMemo(() => {
    const all = task.steps || [];
    const skipped = all.filter((s) => s.status === "skipped").length;
    const { steps } = shownSteps(all, hideSkipped);
    const ns: Node[] = [];
    const es: Edge[] = [];
    const cards: Record<string, NodeCard> = {};
    const edgeStyle = (live: boolean, running = false) => ({
      stroke: running ? "var(--color-primary)" : live ? "var(--color-muted-foreground)" : "var(--color-border)",
      strokeWidth: 2, strokeDasharray: live ? undefined : "6 4",
    });
    const plannerLook = taskLook(task.status, "Planning and deciding");
    ns.push({
      id: "planner", position: { x: -NODE_W / 2, y: 0 },
      data: { label: <NodeLabel name="Planner" look={plannerLook} /> },
      style: nodeStyle(plannerLook), sourcePosition: Position.Bottom, targetPosition: Position.Top,
    });
    cards.planner = plannerCard(task, plannerLook);

    if (all.length === 0) {
      const look = { ...STEP_LOOKS.pending, borderStyle: "dashed" as const, label: "Waiting for the plan" };
      ns.push({
        id: "waiting", position: { x: -NODE_W / 2, y: ROW_H },
        data: { label: <NodeLabel name="Steps" look={look} /> },
        style: nodeStyle(look), sourcePosition: Position.Bottom, targetPosition: Position.Top,
      });
      cards.waiting = { name: "Steps", look, roles: [],
                        sections: [{ kind: "text", title: "Plan", text: "The Planner has not planned the steps yet." }] };
      es.push({ id: "e-planner-waiting", source: "planner", target: "waiting", animated: true, style: edgeStyle(true) });
      return { nodes: described(ns), edges: es, rows: 2, cards, skipped };
    }

    // The steps drawn, laid out without the hidden ones; a card still names every step it depends on.
    const byId = new Map(steps.map((s) => [s.id, s]));
    const allById = new Map(all.map((s) => [s.id, s]));
    const gates = new Map((task.gates ?? []).map((gate) => [gate.step_id, gate]));
    const active = isActiveTask(task);
    // Each step's look as its node draws it: a gate's by its gate (whose turn it is while it is discussed, the
    // Planner's only while the task runs, as the gate's panel says).
    const lookOf = (step: TaskStep): NodeLook => {
      if (step.kind !== "gate") return stepLook(step.status);
      const gate = gates.get(step.id);
      const turn = gate && gateTurn(gate, active);
      return turn ? GATE_TURN_LOOKS[turn] : GATE_LOOKS[gateState(step, gate)];
    };
    const levels = stepLevels(steps);
    const why = droppedBecause(task.journal || [], all);
    const x = stepColumns(steps, levels, NODE_W + GAP_X);
    const depth = steps.length ? Math.max(...levels.values()) : -1;

    const dependedOn = new Set(steps.filter((s) => s.status !== "skipped").flatMap((s) => s.depends_on ?? []));
    const ends = steps.filter((s) => s.status !== "skipped" && !dependedOn.has(s.id));
    for (const step of steps) {
      const name = step.step_key || `step ${step.step_index + 1}`;
      const deps = (step.depends_on ?? []).filter((d) => byId.has(d));
      const position = { x: (x.get(step.id) ?? 0) - NODE_W / 2, y: ROW_H * (1 + (levels.get(step.id) ?? 0)) };
      const live = step.status !== "skipped";
      if (step.kind === "gate") {
        const look = lookOf(step);
        ns.push({
          id: step.id, type: "gate", position, focusable: false,
          data: { name, topic: step.instruction, look, onOpen: () => onOpenGate(step) },
          style: { width: NODE_W, height: NODE_H, padding: 0, background: "transparent", border: "none" },
        });
        cards[step.id] = gateCard(step, gates.get(step.id), { look, lookOf, stepsById: allById, active });
      }
      for (const source of deps.length ? deps : ["planner"]) {
        const fromLive = source === "planner" || byId.get(source)?.status !== "skipped";
        const running = step.status === "dispatched";
        es.push({ id: `dep:${source}->${step.id}`, source, target: step.id, animated: running,
                  style: edgeStyle(live && fromLive, running) });
      }
      if (step.kind === "gate") continue;
      const look = stepLook(step.status);
      ns.push({
        id: step.id,
        position,
        data: { label: <NodeLabel name={name} look={look} role={stepRoles(step, false) || undefined}
                                  detail={step.instruction} note={step.retries ? `retry ${step.retries}` : undefined} /> },
        style: nodeStyle(look), sourcePosition: Position.Bottom, targetPosition: Position.Top,
      });
      cards[step.id] = stepCard(step, { taskId: task.task_id, look, lookOf, stepsById: allById,
                                        skippedBecause: why.get(step.id) });
    }

    // The result sits under the steps that lead to it, so its edges do not cross the dropped steps above it.
    const resultX = ends.length ? ends.reduce((sum, s) => sum + (x.get(s.id) ?? 0), 0) / ends.length : 0;
    const resultLook = taskLook(task.status, "In progress");
    ns.push({
      id: "result", position: { x: resultX - NODE_W / 2, y: ROW_H * (2 + depth) },
      data: { label: <NodeLabel name="Result" look={resultLook} /> },
      style: nodeStyle(resultLook), targetPosition: Position.Top,
    });
    cards.result = resultCard(task, resultLook);
    for (const step of ends) {
      es.push({ id: `end:${step.id}`, source: step.id, target: "result", style: edgeStyle(true) });
    }
    return { nodes: described(ns), edges: es, rows: depth + 3, cards, skipped };
  }, [task, hideSkipped, onOpenGate]);
  const changeHideSkipped = (hide: boolean) => {
    setHideSkipped(hide);
    storeHideSkipped(hide);
  };

  return (
    // Taller for a deeper plan, so its labels stay readable before any zoom; full screen, the whole screen (the
    // browser's, or a fixed overlay where the browser has none).
    <div
      ref={container}
      className={cn("w-full bg-background", full === "overlay" && "fixed inset-0 z-50")}
      style={full === "off" ? { height: Math.min(860, Math.max(420, rows * 90)) } : undefined}
    >
      <ReactFlow
        style={PIPELINE_THEME}
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        fitViewOptions={{ padding: FIT_PADDING }}
        minZoom={0.15}
        colorMode="dark"
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        // A quick second tap on a node closes its card; it does not zoom too.
        zoomOnDoubleClick={false}
        defaultEdgeOptions={{ type: "smoothstep" }}
      >
        <Background color="var(--color-border)" gap={20} />
        <Controls showInteractive={false} />
        <NodeCards cards={cards} />
        <PipelineToolbar skipped={skipped} hideSkipped={hideSkipped} onHideSkipped={changeHideSkipped}
                         full={full !== "off"} onToggleFull={toggleFull} />
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
  /** Start over waits for its confirmation: it deletes the task's steps and plan. */
  const [confirmStartOver, setConfirmStartOver] = useState(false);
  const [tab, setTab] = useState<Tab>("overview");
  // Bumped when the task runs again, so the page polls it again.
  const [runs, setRuns] = useState(0);
  // The gate step a Pipeline node asked to show on the Overview tab, until it is shown.
  const [gateFocus, setGateFocus] = useState<TaskStep | null>(null);
  const openGate = useCallback((step: TaskStep) => {
    setGateFocus(step);
    setTab("overview");
  }, []);
  const gateShown = useCallback(() => setGateFocus(null), []);

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

  // A ready task is started; a stopped, failed or cancelled one goes on from its saved plan, or is planned anew.
  const start = (startOver: boolean) =>
    (task?.status === READY_STATUS ? startReadyTask(taskId) : startTask(taskId, { startOver })).then(
      () => { setStartError(""); setConfirmStartOver(false); setRuns((n) => n + 1); },
      (err) => setStartError(err instanceof Error ? err.message : String(err)),
    );

  const tabs: { id: Tab; label: string; icon: React.ElementType }[] = [
    { id: "overview", label: "Overview", icon: Activity },
    { id: "pipeline", label: "Pipeline", icon: GitBranch },
    { id: "agents", label: "Agents", icon: Bot },
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
          {task && confirmStartOver && (
            <div className="flex shrink-0 items-center gap-1.5">
              <span className="text-xs text-muted-foreground">Delete its steps?</span>
              <button
                type="button"
                onClick={() => start(true)}
                title="Delete the task's steps, plan and Planner session, then plan it anew from its requirement"
                className="rounded-md px-3 py-1.5 text-xs font-medium bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-colors"
              >
                Start over
              </button>
              <button
                type="button"
                onClick={() => setConfirmStartOver(false)}
                className="rounded-md px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
              >
                No
              </button>
            </div>
          )}
          {task && !confirmStartOver && ["stopped", "failed", "cancelled", "init"].includes(task.status) && (
            <>
              <button
                type="button"
                onClick={() => start(false)}
                title={task.status === READY_STATUS ? undefined : "Continue from the saved plan: accepted steps are kept"}
                className="flex items-center gap-1.5 rounded-md border border-success/50 px-3 py-1.5 text-xs font-medium text-success hover:bg-success/10 transition-colors"
              >
                <Play className="h-3.5 w-3.5" />
                Start
              </button>
              {task.status !== READY_STATUS && (
                <button
                  type="button"
                  onClick={() => setConfirmStartOver(true)}
                  aria-label="Start over"
                  title="Start over: delete its steps and plan it anew"
                  className="flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  <span className="hidden sm:inline">Start over</span>
                </button>
              )}
            </>
          )}
        </div>
        {startError && <p className="mt-1 text-xs text-destructive">{startError}</p>}

        {/* Tabs */}
        <div className="flex flex-wrap gap-1 mt-3">
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
            onOpenAgents={() => setTab("agents")}
            onRoleEdited={() => setRuns((n) => n + 1)}
            onGateChanged={(gate) => {
              // Show whom the gate waits for now, then read the whole task again.
              setTask((t) => (t ? { ...t, gates: t.gates.map((g) => (g.step_id === gate.step_id ? gate : g)) } : t));
              setRuns((n) => n + 1);
            }}
            gateFocus={gateFocus}
            onGateShown={gateShown}
          />
        )}
        {task && tab === "pipeline" && <PipelineTab task={task} onOpenGate={openGate} />}
        {task && tab === "agents" && (
          <TaskAgentsTab task={task} onAgents={(agents) => setTask((t) => (t ? { ...t, agents } : t))} />
        )}
        {task && tab === "sessions" && <SessionsTab taskId={taskId} />}
        {task && tab === "conversation" && <ConversationTab taskId={taskId} />}
      </div>
    </div>
  );
}
