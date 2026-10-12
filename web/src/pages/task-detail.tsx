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
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import {
  ReactFlow,
  Background,
  type Node,
  type Edge,
  Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  fetchTaskDetail,
  fetchTaskActivity,
  fetchTaskConversation,
  streamTaskActivity,
  stopTask,
  startTask,
  type TaskDetail,
  type TaskStep,
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

function OverviewTab({ task }: { task: TaskDetail }) {
  const steps = task.steps || [];
  const done = steps.filter((s) => s.status === "completed").length;
  const total = steps.length;
  const isActive = task.status === "running" || task.status === "pending";

  return (
    <div className="space-y-6 p-5">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
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
      </div>

      <div>
        <h3 className="text-sm font-semibold mb-2">Requirement</h3>
        <div className="rounded-lg border border-border bg-card px-4 py-3">
          <p className="text-sm whitespace-pre-wrap">{task.requirement}</p>
        </div>
      </div>

      {task.result_summary && (
        <div>
          <h3 className="text-sm font-semibold mb-2">Result</h3>
          <div className="rounded-lg border border-success/30 bg-success/5 px-4 py-3">
            <p className="text-sm whitespace-pre-wrap">{task.result_summary}</p>
          </div>
        </div>
      )}

      {task.error && (
        <div>
          <h3 className="text-sm font-semibold mb-2">Error</h3>
          <div className="rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3">
            <p className="text-sm whitespace-pre-wrap text-destructive">{task.error}</p>
          </div>
        </div>
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
                        <span className="text-xs text-muted-foreground">
                          {step.worker_id}
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

      <AgentTimeline taskId={task.task_id} />

      <LiveActivityFeed taskId={task.task_id} isActive={isActive} />
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
              <button
                type="button"
                onClick={() => toggle(agent)}
                className="w-full text-left px-4 py-3 hover:brightness-110 transition-all"
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
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
                      {activityKindIcon(ev.kind)}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-[10px] text-muted-foreground/60">
                            {formatTime(ev.created_at)}
                          </span>
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
      data: { label: `Planner ${task.planner_id ? `(${task.planner_id.slice(0, 20)})` : ""}` },
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
          data: { label: `Step ${i + 1}: ${step.instruction.slice(0, 30)}${step.instruction.length > 30 ? "..." : ""}` },
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
            data: { label: step.worker_id.slice(0, 20) },
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

// --- Main Page ---

interface TaskDetailPageProps {
  taskId: string;
  onBack: () => void;
}

export function TaskDetailPage({ taskId, onBack }: TaskDetailPageProps) {
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState<Tab>("overview");

  useEffect(() => {
    let timer: ReturnType<typeof setInterval>;
    let stopped = false;
    const load = () =>
      fetchTaskDetail(taskId)
        .then((t) => {
          if (stopped) return;
          setTask(t);
          setError("");
          if (["completed", "failed", "cancelled", "stopped"].includes(t.status)) {
            clearInterval(timer);
          }
        })
        .catch((e) => { if (!stopped) setError(e.message); });
    load();
    timer = setInterval(load, 2000);
    return () => { stopped = true; clearInterval(timer); };
  }, [taskId]);

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
            className="flex h-8 w-8 items-center justify-center rounded-md hover:bg-accent transition-colors"
          >
            <ArrowLeft className="h-4 w-4" />
          </button>
          <div className="min-w-0 flex-1">
            <h1 className="text-lg font-semibold leading-tight truncate">
              {task?.requirement || taskId}
            </h1>
            <div className="flex items-center gap-2 mt-0.5">
              <span className="text-xs text-muted-foreground">{taskId}</span>
              {task && taskStatusBadge(task.status)}
              {task && task.phase && task.phase !== task.status && phaseBadge(task.phase)}
            </div>
          </div>
          {task && ["running", "pending", "executing", "assigned"].includes(task.status) && (
            <button
              type="button"
              onClick={() => stopTask(taskId).then(() => {})}
              className="flex items-center gap-1.5 rounded-md border border-warning/50 px-3 py-1.5 text-xs font-medium text-warning hover:bg-warning/10 transition-colors"
            >
              <Pause className="h-3.5 w-3.5" />
              Stop
            </button>
          )}
          {task && ["stopped", "failed", "cancelled"].includes(task.status) && (
            <button
              type="button"
              onClick={() => startTask(taskId).then(() => {})}
              className="flex items-center gap-1.5 rounded-md border border-success/50 px-3 py-1.5 text-xs font-medium text-success hover:bg-success/10 transition-colors"
            >
              <Play className="h-3.5 w-3.5" />
              Start
            </button>
          )}
        </div>

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

        {task && tab === "overview" && <OverviewTab task={task} />}
        {task && tab === "pipeline" && <PipelineTab task={task} />}
        {task && tab === "sessions" && <SessionsTab taskId={taskId} />}
        {task && tab === "conversation" && <ConversationTab taskId={taskId} />}
      </div>
    </div>
  );
}
