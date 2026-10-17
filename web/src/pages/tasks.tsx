import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Trash2,
  RefreshCw,
  Clock,
  CheckCircle2,
  AlertCircle,
  Loader2,
  Play,
  Pause,
  Plus,
  RotateCcw,
  Settings2,
  Folder,
  MessageCircleQuestionMark,
  X,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  fetchSchedule,
  fetchTasks,
  deleteTask,
  stopTask,
  startTask,
  startReadyTask,
  createTask,
  READY_LABEL,
  READY_STATUS,
  isActiveTask,
  isTaskGroup,
  TASK_GROUPS,
  type TaskGroup,
  type TaskInfo,
  type TaskRole,
} from "@/lib/api";
import { useProjects } from "@/lib/use-projects";
import { AgentProfileSelect } from "@/components/agent-profiles";
import { ScheduledBadge } from "@/components/scheduled-badge";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { formatUsd } from "@/lib/usage";
import { ProjectBackgroundField, backgroundTooLong } from "@/components/project-background-field";
import { TaskDetailRoute } from "./task-detail";

const STATUS_FILTERS = [
  "all", "init", "running", "waiting_user", "pending", "completed", "failed", "cancelled", "stopped",
] as const;
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
    case READY_STATUS:
      return <Badge variant="outline">{READY_LABEL}</Badge>;
    case "executing":
      return <Badge variant="default">Executing</Badge>;
    case "waiting_user":
      return <Badge variant="warning">Waiting for you</Badge>;
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
    case "waiting_user":
      return <MessageCircleQuestionMark className="h-4 w-4 text-warning" />;
    case READY_STATUS:
      return <Settings2 className="h-4 w-4 text-muted-foreground" />;
    default:
      return <Clock className="h-4 w-4 text-muted-foreground" />;
  }
}

const RESTARTABLE_STATUSES = ["stopped", "failed", "cancelled", READY_STATUS];

/** A status filter's name. */
function filterLabel(filter: string): string {
  if (filter === READY_STATUS) return "Ready";
  if (filter === "waiting_user") return "Waiting for you";
  return filter.charAt(0).toUpperCase() + filter.slice(1);
}

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

/** The create form's project choice that reveals the new-project fields. */
const NEW_PROJECT = "__new__";

const TASK_ROLES: { id: TaskRole; label: string }[] = [
  { id: "planner", label: "Planner" },
  { id: "worker", label: "Worker" },
  { id: "reviewer", label: "Reviewer" },
];

type ProfileChoice = Partial<Record<TaskRole, string>>;

/** The Agent Profile of each of the task's roles: from the settings (nothing chosen), or one for this task. */
function TaskProfilesField({ value, onChange }: { value: ProfileChoice; onChange: (value: ProfileChoice) => void }) {
  return (
    <fieldset className="space-y-2">
      <legend className="text-sm font-medium text-muted-foreground">{AGENT_PROFILE.plural}</legend>
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-2">
        {TASK_ROLES.map((role) => (
          <div key={role.id} className="contents">
            <label htmlFor={`new-task-profile-${role.id}`} className="pt-2 text-xs text-muted-foreground">{role.label}</label>
            <AgentProfileSelect
              id={`new-task-profile-${role.id}`}
              value={value[role.id] ?? ""}
              onChange={(profile) => onChange({ ...value, [role.id]: profile })}
              emptyLabel="From settings"
              roles={[role.id]}
              className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
            />
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        Optional. A chosen {AGENT_PROFILE.name} runs that role for this task only, over the project&apos;s{" "}
        {AGENT_PROFILE.plural} and Settings → Roles.
      </p>
    </fieldset>
  );
}

interface TasksPageProps {
  sub?: string;
  /** "project=<id>" filters the list by project, "group=<group>" by a task group (the Dashboard's counts),
   * "schedule=<id>" by the schedule that created the tasks. */
  query?: string;
  onNavigate?: (sub: string, query?: string) => void;
}

function filterQuery(project: string, group: TaskGroup | null, schedule = ""): string {
  const query = new URLSearchParams();
  if (project) query.set("project", project);
  if (group) query.set("group", group);
  if (schedule) query.set("schedule", schedule);
  return query.toString();
}

/** Whether task `a` comes after `b` in the list (newest first: by creation, then id). */
function isOlder(a: TaskInfo, b: TaskInfo): boolean {
  const at = Date.parse(a.created_at);
  const bt = Date.parse(b.created_at);
  return at < bt || (at === bt && a.task_id < b.task_id);
}

/** The newest page read again, then the tasks of the older pages already shown that come after it. */
function withOlder(newest: TaskInfo[], current: TaskInfo[]): TaskInfo[] {
  const last = newest[newest.length - 1];
  if (!last) return newest;
  const shown = new Set(newest.map((t) => t.task_id));
  return [...newest, ...current.filter((t) => !shown.has(t.task_id) && isOlder(t, last))];
}

export function TasksPage({ sub, query, onNavigate }: TasksPageProps) {
  const params = new URLSearchParams(query || "");
  const projectFilter = params.get("project") || "";
  const groupParam = params.get("group");
  const groupFilter = isTaskGroup(groupParam) ? groupParam : null;
  const group = TASK_GROUPS.find((g) => g.id === groupFilter) ?? null;
  const scheduleFilter = params.get("schedule") || "";
  const [filter, setFilter] = useState<StatusFilter>("all");
  // What the list shows: the server filters by all four.
  const listKey = `${filterQuery(projectFilter, groupFilter, scheduleFilter)}|${filter}`;
  const { projects, reload: reloadProjects } = useProjects();
  const selectedProject = projects?.find((p) => p.project_id === projectFilter) ?? null;
  // A filter for a project the list does not hold yet (just created elsewhere) is
  // reported missing only after a fresh load confirms it.
  const projectMissing = !!projectFilter && !!projects && !selectedProject;
  const [confirmedMissing, setConfirmedMissing] = useState("");
  useEffect(() => {
    if (!projectMissing) return;
    let alive = true;
    reloadProjects().then(() => {
      if (alive) setConfirmedMissing(projectFilter);
    });
    return () => {
      alive = false;
    };
  }, [projectMissing, projectFilter, reloadProjects]);
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  // The list the last finished load was for; anything else is still loading.
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const loading = loadedFor !== listKey;
  const [error, setError] = useState("");
  /** Continues the list (null at its end), and how many tasks each status holds under the URL's filters. */
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [loadingMore, setLoadingMore] = useState(false);
  /** Whether older pages were loaded for the list `key`: a refresh then keeps them below the newest page. */
  const older = useRef({ key: "", loaded: false });
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  /** The task whose Start over waits for its confirmation: it deletes the task's steps and plan. */
  const [confirmStartOver, setConfirmStartOver] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [newReq, setNewReq] = useState("");
  const [newWorkDir, setNewWorkDir] = useState("");
  const [newProject, setNewProject] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDir, setNewProjectDir] = useState("");
  const [newProjectDesc, setNewProjectDesc] = useState("");
  const [newProjectBackground, setNewProjectBackground] = useState("");
  const [newProfiles, setNewProfiles] = useState<ProfileChoice>({});
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  const [scheduleNames, setScheduleNames] = useState<Record<string, string>>({});
  const selectedTaskId = sub || null;
  // The list's filter, so leaving a task's detail returns to the list it was opened from.
  const listQuery = useRef("");
  useEffect(() => {
    if (!sub) listQuery.current = query || "";
  }, [sub, query]);
  const setSelectedTaskId = (id: string | null) =>
    id ? onNavigate?.(id) : onNavigate?.("", listQuery.current);
  const setProjectFilter = (id: string) => onNavigate?.("", filterQuery(id, groupFilter, scheduleFilter));
  const clearGroup = () => onNavigate?.("", filterQuery(projectFilter, null, scheduleFilter));
  const clearSchedule = () => onNavigate?.("", filterQuery(projectFilter, groupFilter));

  // Drop answers for a filter the page has already left.
  const currentFilter = useRef(listKey);
  useEffect(() => {
    currentFilter.current = listKey;
  }, [listKey]);

  const filters = {
    projectId: projectFilter || undefined,
    group: groupFilter ?? undefined,
    scheduleId: scheduleFilter || undefined,
    status: filter === "all" ? undefined : filter,
  };

  /** Read the newest page again; older pages loaded for this list stay below it. */
  const load = () => {
    const requested = listKey;
    fetchTasks(filters)
      .then((page) => {
        if (requested !== currentFilter.current) return;
        setCounts(page.counts);
        if (older.current.key === requested && older.current.loaded) {
          setTasks((current) => withOlder(page.tasks, current));
        } else {
          older.current = { key: requested, loaded: false };
          setTasks(page.tasks);
          setNextBefore(page.next_before);
        }
        setError("");
      })
      .catch((e) => {
        if (requested === currentFilter.current) setError(e.message);
      })
      .finally(() => {
        if (requested === currentFilter.current) setLoadedFor(requested);
      });
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, [listKey]);

  const loadMore = () => {
    if (!nextBefore) return;
    const requested = listKey;
    setLoadingMore(true);
    fetchTasks({ ...filters, before: nextBefore })
      .then((page) => {
        if (requested !== currentFilter.current) return;
        older.current = { key: requested, loaded: true };
        setTasks((current) => {
          const shown = new Set(current.map((t) => t.task_id));
          return [...current, ...page.tasks.filter((t) => !shown.has(t.task_id))];
        });
        setNextBefore(page.next_before);
        setCounts(page.counts);
        setError("");
      })
      .catch((e) => requested === currentFilter.current && setError(e.message))
      .finally(() => setLoadingMore(false));
  };

  const handleStop = async (taskId: string) => {
    try {
      await stopTask(taskId);
      load();
    } catch {
      // ignore
    }
  };

  const handleStart = async (task: TaskInfo, startOver = false) => {
    try {
      // A ready task is started; a stopped, failed or cancelled one goes on from its saved plan, or is planned anew.
      await (task.status === READY_STATUS ? startReadyTask(task.task_id) : startTask(task.task_id, { startOver }));
      setConfirmStartOver(null);
      load();
    } catch {
      // ignore
    }
  };

  const handleDelete = async (taskId: string) => {
    const task = tasks.find((t) => t.task_id === taskId);
    if (task && isActiveTask(task)) {
      await handleStop(taskId);
      await new Promise((r) => setTimeout(r, 500));
    }
    try {
      await deleteTask(taskId);
      setConfirmDelete(null);
      setTasks((current) => current.filter((t) => t.task_id !== taskId));
      load();
    } catch {
      // ignore
    }
  };

  const chosenProject = projects?.find((p) => p.project_id === newProject) ?? null;
  const isNewProject = newProject === NEW_PROJECT;
  const projectChosen = isNewProject ? !!newProjectName.trim() : !!chosenProject;
  /** The directory a task without its own work_dir would run in. */
  const defaultWorkDir = isNewProject ? newProjectDir.trim() : chosenProject?.work_dir || "";
  const canCreate =
    !!newReq.trim() &&
    projectChosen &&
    (!!newWorkDir.trim() || !!defaultWorkDir) &&
    !(isNewProject && backgroundTooLong(newProjectBackground));

  const resetCreate = () => {
    setShowCreate(false);
    setCreateError("");
    setNewReq("");
    setNewWorkDir("");
    setNewProjectName("");
    setNewProjectDir("");
    setNewProjectDesc("");
    setNewProjectBackground("");
    setNewProfiles({});
  };

  const openCreate = () => {
    setNewProject(selectedProject?.project_id ?? (projects?.length === 0 ? NEW_PROJECT : ""));
    setShowCreate(true);
  };

  const handleCreate = async () => {
    if (!canCreate) return;
    setCreating(true);
    setCreateError("");
    // An empty directory is left out so the project's default applies; so is a role without a chosen Agent Profile.
    const overrides = Object.fromEntries(Object.entries(newProfiles).filter(([, profileId]) => profileId));
    const base = {
      requirement: newReq.trim(),
      work_dir: newWorkDir.trim() || undefined,
      profile_overrides: Object.keys(overrides).length ? overrides : undefined,
    };
    try {
      const result = await createTask(
        isNewProject
          ? {
              ...base,
              project: {
                name: newProjectName.trim(),
                description: newProjectDesc.trim() || undefined,
                work_dir: newProjectDir.trim() || undefined,
                background: newProjectBackground.trim() ? newProjectBackground : undefined,
              },
            }
          : { ...base, project_id: newProject },
      );
      resetCreate();
      load();
      setSelectedTaskId(result.task_id);
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  // The server filters by project and schedule too; this keeps the list right while a new filter loads.
  const projectTasks = tasks.filter(
    (t) => (!projectFilter || t.project_id === projectFilter) && (!scheduleFilter || t.schedule_id === scheduleFilter),
  );
  // The schedule filter's name: from its tasks, else asked once (an unknown schedule shows its id).
  const scheduleName = scheduleFilter
    ? projectTasks.find((t) => t.schedule_name)?.schedule_name || scheduleNames[scheduleFilter] || scheduleFilter
    : "";
  const nameKnown = !scheduleFilter || projectTasks.some((t) => t.schedule_name) || scheduleFilter in scheduleNames;
  useEffect(() => {
    if (nameKnown || loading) return;
    let alive = true;
    const asked = scheduleFilter;
    fetchSchedule(asked)
      .then((schedule) => alive && setScheduleNames((names) => ({ ...names, [asked]: schedule.name })))
      .catch(() => alive && setScheduleNames((names) => ({ ...names, [asked]: "" })));
    return () => {
      alive = false;
    };
  }, [nameKnown, loading, scheduleFilter]);
  const filtered = filter === "all" ? projectTasks : projectTasks.filter((t) => t.status === filter);
  // Every task under the URL's filters, not only the pages loaded.
  const total = Object.values(counts).reduce((sum, n) => sum + n, 0);
  const shownTotal = filter === "all" ? total : counts[filter] || 0;

  if (selectedTaskId) {
    return (
      <TaskDetailRoute
        taskId={selectedTaskId}
        onBack={() => setSelectedTaskId(null)}
      />
    );
  }

  return (
    <div className="space-y-6 p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Tasks</h1>
          <p className="text-sm text-muted-foreground mt-1">
            {group ? `${total} tasks · ${group.label}` : `${total} total tasks`}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={openCreate}
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

      {/* Project and status filters */}
      <div className="flex items-center gap-3 flex-wrap">
        <select
          aria-label="Project"
          value={projectFilter}
          onChange={(e) => setProjectFilter(e.target.value)}
          className="h-8 max-w-[14rem] rounded-md border border-input bg-transparent px-2 text-xs font-medium text-foreground"
        >
          <option value="">All projects</option>
          {projects?.map((p) => (
            <option key={p.project_id} value={p.project_id}>{p.name}</option>
          ))}
          {projectFilter && !selectedProject && <option value={projectFilter}>{projectFilter}</option>}
        </select>
        {group && (
          <span className="inline-flex h-8 items-center gap-1 rounded-md bg-primary/10 pl-3 pr-1 text-xs font-medium text-primary ring-1 ring-inset ring-primary/20">
            {group.label}
            <button
              type="button"
              onClick={clearGroup}
              aria-label={`Show all tasks, not only ${group.label.toLowerCase()}`}
              className="flex h-6 w-6 items-center justify-center rounded-sm hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </span>
        )}
        {scheduleFilter && (
          <span className="inline-flex h-8 max-w-full items-center gap-1 rounded-md bg-primary/10 pl-3 pr-1 text-xs font-medium text-primary ring-1 ring-inset ring-primary/20">
            <span className="truncate">Schedule: {scheduleName}</span>
            <button
              type="button"
              onClick={clearSchedule}
              aria-label={`Show all tasks, not only those of the schedule ${scheduleName}`}
              className="flex h-6 w-6 shrink-0 items-center justify-center rounded-sm hover:bg-primary/20 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              <X className="h-3.5 w-3.5" aria-hidden="true" />
            </button>
          </span>
        )}
        <div className="flex items-center gap-1 flex-wrap">
          {STATUS_FILTERS.map((f) => {
            const count = f === "all" ? total : counts[f] || 0;
            // Only a project with user gates has tasks that wait for the user.
            if (f === "waiting_user" && count === 0 && filter !== f) return null;
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
                {f === "all" ? "All" : filterLabel(f)}
                {count > 0 && <span className="ml-1.5 opacity-70">{count}</span>}
              </button>
            );
          })}
        </div>
      </div>

      {selectedProject ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card px-5 py-3">
          <p className="flex min-w-0 items-center gap-2 text-sm">
            <Folder className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className="truncate font-medium">{selectedProject.name}</span>
            {selectedProject.work_dir && (
              <span className="hidden truncate font-mono text-xs text-muted-foreground sm:inline">
                {selectedProject.work_dir}
              </span>
            )}
          </p>
          <a
            href={`#projects/${selectedProject.project_id}`}
            className="inline-flex items-center gap-1 rounded-sm text-xs font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            Project page
            <ArrowRight className="h-3 w-3" aria-hidden="true" />
          </a>
        </div>
      ) : (
        projectMissing &&
        confirmedMissing === projectFilter && (
          <div className="flex items-center justify-between gap-4 rounded-lg border border-border bg-card px-5 py-4 text-sm text-muted-foreground">
            <span>
              Project <span className="font-mono">{projectFilter}</span> does not exist.
            </span>
            <Button type="button" variant="outline" size="sm" onClick={() => setProjectFilter("")}>
              Show all projects
            </Button>
          </div>
        )
      )}

      {error && !tasks.length && (
        <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-5 py-4 text-sm text-destructive">
          {error}
        </div>
      )}

      {loading && !projectTasks.length ? (
        <div className="flex h-64 items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex h-64 items-center justify-center">
          <p className="text-sm text-muted-foreground">
            {filter !== "all"
              ? `No ${filterLabel(filter).toLowerCase()} tasks`
              : group
                ? `No tasks · ${group.label}`
                : scheduleFilter
                  ? "No tasks from this schedule"
                  : "No tasks yet"}
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
                    <p className="text-sm font-medium truncate">{task.name || task.requirement}</p>
                    <div className="mt-1.5 flex items-center gap-2 flex-wrap">
                      {statusBadge(task.status)}
                      <ScheduledBadge task={task} />
                      {!projectFilter && task.project_name && (
                        <span className="flex items-center gap-1 text-xs text-muted-foreground">
                          <Folder className="h-3 w-3" />
                          {task.project_name}
                        </span>
                      )}
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
                      {task.cost && task.cost.runs > 0 && (
                        <span
                          className="text-xs text-muted-foreground font-mono"
                          title={`What its Planner, Workers and Reviewers spent over ${task.cost.runs} run${task.cost.runs === 1 ? "" : "s"}`}
                        >
                          {formatUsd(task.cost.cost_usd)}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
                <div className="shrink-0 flex items-center gap-1">
                  {isActiveTask(task) && (
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
                  {confirmStartOver === task.task_id ? (
                    <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                      <span className="text-xs text-muted-foreground">Delete its steps?</span>
                      <button
                        type="button"
                        onClick={() => handleStart(task, true)}
                        title="Delete the task's steps, plan and Planner session, then plan it anew from its requirement"
                        className="rounded-md px-2 py-1 text-xs bg-destructive text-destructive-foreground hover:bg-destructive/90 transition-colors"
                      >
                        Start over
                      </button>
                      <button
                        type="button"
                        onClick={() => setConfirmStartOver(null)}
                        className="rounded-md px-2 py-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                      >
                        No
                      </button>
                    </div>
                  ) : (
                    RESTARTABLE_STATUSES.includes(task.status) && (
                      <>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleStart(task);
                          }}
                          title={task.status === READY_STATUS ? undefined : "Continue from the saved plan: accepted steps are kept"}
                          className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-success hover:bg-success/10 transition-colors"
                        >
                          <Play className="h-3.5 w-3.5" />
                          Start
                        </button>
                        {task.status !== READY_STATUS && (
                          <button
                            type="button"
                            onClick={(e) => {
                              e.stopPropagation();
                              setConfirmDelete(null);
                              setConfirmStartOver(task.task_id);
                            }}
                            aria-label="Start over"
                            title="Start over: delete its steps and plan it anew"
                            className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                          >
                            <RotateCcw className="h-3.5 w-3.5" />
                            <span className="hidden sm:inline">Start over</span>
                          </button>
                        )}
                      </>
                    )
                  )}
                  {confirmStartOver === task.task_id ? null : confirmDelete === task.task_id ? (
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
                        setConfirmStartOver(null);
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
      {!loading && filtered.length > 0 && (nextBefore || filtered.length < shownTotal) && (
        <div className="flex flex-wrap items-center justify-center gap-3 text-xs text-muted-foreground">
          <span>
            Showing {filtered.length} of {shownTotal}
          </span>
          {nextBefore && (
            <Button type="button" variant="outline" size="sm" onClick={loadMore} disabled={loadingMore} className="h-11 sm:h-8">
              {loadingMore && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
              Load more
            </Button>
          )}
        </div>
      )}

      {/* Create task modal */}
      {showCreate && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
          <div className="w-full max-w-lg max-h-full overflow-y-auto rounded-xl border border-border bg-card p-6 shadow-xl">
            <h2 className="text-lg font-semibold mb-4">Create New Task</h2>
            <div className="space-y-4">
              <div>
                <label htmlFor="new-task-project" className="text-sm font-medium text-muted-foreground">Project *</label>
                <select
                  id="new-task-project"
                  value={newProject}
                  onChange={(e) => setNewProject(e.target.value)}
                  className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
                >
                  <option value="" disabled>Choose a project…</option>
                  {projects?.map((p) => (
                    <option key={p.project_id} value={p.project_id}>{p.name}</option>
                  ))}
                  <option value={NEW_PROJECT}>＋ New project…</option>
                </select>
              </div>
              {isNewProject && (
                <div className="space-y-3 rounded-lg border border-border p-3">
                  <div className="grid gap-3 sm:grid-cols-2">
                    <div>
                      <label htmlFor="new-task-project-name" className="text-xs font-medium text-muted-foreground">Project name *</label>
                      <input
                        id="new-task-project-name"
                        type="text"
                        value={newProjectName}
                        maxLength={80}
                        onChange={(e) => setNewProjectName(e.target.value)}
                        placeholder="my-app"
                        className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
                      />
                    </div>
                    <div>
                      <label htmlFor="new-task-project-dir" className="text-xs font-medium text-muted-foreground">Default directory</label>
                      <input
                        id="new-task-project-dir"
                        type="text"
                        value={newProjectDir}
                        onChange={(e) => setNewProjectDir(e.target.value)}
                        placeholder="/path/to/project"
                        className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/50"
                      />
                    </div>
                  </div>
                  <div>
                    <label htmlFor="new-task-project-desc" className="text-xs font-medium text-muted-foreground">Description</label>
                    <input
                      id="new-task-project-desc"
                      type="text"
                      value={newProjectDesc}
                      onChange={(e) => setNewProjectDesc(e.target.value)}
                      className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50"
                    />
                  </div>
                  <ProjectBackgroundField
                    id="new-task-project-background"
                    value={newProjectBackground}
                    onChange={setNewProjectBackground}
                    rows={4}
                  />
                </div>
              )}
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
                <label className="text-sm font-medium text-muted-foreground">
                  Work Directory{defaultWorkDir ? "" : " *"}
                </label>
                <input
                  type="text"
                  value={newWorkDir}
                  onChange={(e) => setNewWorkDir(e.target.value)}
                  placeholder={defaultWorkDir ? `Default: ${defaultWorkDir}` : "~/projects/my-app"}
                  className="mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-primary/50"
                />
                <p className="mt-1 text-xs text-muted-foreground">
                  {defaultWorkDir
                    ? "Leave empty to use the project's default directory. "
                    : projectChosen
                      ? `Required: ${isNewProject ? "the new project" : "this project"} has no default directory. `
                      : ""}
                  Where the task's Workers run: an absolute path or one starting with ~/. Created if it does not exist.
                </p>
              </div>
              <TaskProfilesField value={newProfiles} onChange={setNewProfiles} />
              <p className="text-xs text-muted-foreground">
                The task is created <strong>ready, not started</strong>: review and edit its configuration, then press Start.
              </p>
              {createError && <p className="text-xs text-destructive">{createError}</p>}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={resetCreate}
                  className="rounded-lg border border-border px-4 py-2 text-sm text-muted-foreground hover:text-foreground transition-colors"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={handleCreate}
                  disabled={!canCreate || creating}
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
