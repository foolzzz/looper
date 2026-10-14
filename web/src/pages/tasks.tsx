import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
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
  Folder,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  fetchTasks,
  cancelTask,
  deleteTask,
  stopTask,
  startTask,
  createTask,
  updateProject,
  deleteProject,
  type Project,
  type ProjectUpdate,
  type TaskInfo,
} from "@/lib/api";
import { useProjects } from "@/lib/use-projects";
import { cn } from "@/lib/utils";
import { formatUsd } from "@/lib/usage";
import { ProjectBackgroundField, backgroundTooLong } from "@/components/project-background-field";
import { ProjectHarnessOverrides, harnessTooLong } from "@/components/harness-field";
import { ProjectKnowledgeSection } from "@/components/project-knowledge";
import { ProjectSkillsSection } from "@/components/project-skills";
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

/** The create form's project choice that reveals the new-project fields. */
const NEW_PROJECT = "__new__";

/** Text cut to three lines, with a toggle when it is longer. */
function ClampedText({ text, className }: { text: string; className?: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el || expanded) return;
    const measure = () => setOverflows(el.scrollHeight > el.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, expanded]);

  return (
    <div>
      <p ref={ref} className={cn("whitespace-pre-wrap break-words", !expanded && "line-clamp-3", className)}>
        {text}
      </p>
      {(overflows || expanded) && (
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          className="mt-1 text-xs font-medium text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

/** The selected project's name, description, default directory, count, background and harness overrides, editable in
 * place; the editor also manages its knowledge and its skills. */
function ProjectBar({ project, onDeleted }: { project: Project; onDeleted: () => void }) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [workDir, setWorkDir] = useState("");
  const [description, setDescription] = useState("");
  const [background, setBackground] = useState("");
  const [plannerHarness, setPlannerHarness] = useState("");
  const [workerHarness, setWorkerHarness] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const startEdit = () => {
    setName(project.name);
    setWorkDir(project.work_dir);
    setDescription(project.description);
    setBackground(project.background);
    setPlannerHarness(project.planner_harness_prompt ?? "");
    setWorkerHarness(project.worker_harness_prompt ?? "");
    setError("");
    setConfirmDelete(false);
    setEditing(true);
  };

  const save = async () => {
    if (!name.trim()) {
      setError("A project name is required.");
      return;
    }
    if (backgroundTooLong(background) || harnessTooLong(plannerHarness) || harnessTooLong(workerHarness)) return;
    // Send only what changed; an empty directory, background or harness clears it.
    const fields: ProjectUpdate = {};
    if (name.trim() !== project.name) fields.name = name.trim();
    if (description.trim() !== project.description) fields.description = description.trim();
    if (workDir.trim() !== project.work_dir) fields.work_dir = workDir.trim();
    if (background !== project.background) fields.background = background;
    if (plannerHarness !== (project.planner_harness_prompt ?? "")) fields.planner_harness_prompt = plannerHarness;
    if (workerHarness !== (project.worker_harness_prompt ?? "")) fields.worker_harness_prompt = workerHarness;
    if (Object.keys(fields).length === 0) {
      setEditing(false);
      return;
    }
    setBusy(true);
    setError("");
    try {
      await updateProject(project.project_id, fields);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setError("");
    try {
      await deleteProject(project.project_id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <div className="rounded-lg border border-border bg-card px-5 py-4 space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <label htmlFor="project-edit-name" className="text-xs font-medium text-muted-foreground">Name *</label>
            <Input id="project-edit-name" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} autoFocus />
          </div>
          <div className="space-y-1">
            <label htmlFor="project-edit-dir" className="text-xs font-medium text-muted-foreground">Default directory</label>
            <Input
              id="project-edit-dir"
              value={workDir}
              onChange={(e) => setWorkDir(e.target.value)}
              placeholder="No default directory"
              className="font-mono"
            />
          </div>
        </div>
        <div className="space-y-1">
          <label htmlFor="project-edit-desc" className="text-xs font-medium text-muted-foreground">Description</label>
          <textarea
            id="project-edit-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            className="w-full resize-none rounded-md border border-input bg-transparent px-3 py-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <ProjectBackgroundField id="project-edit-background" value={background} onChange={setBackground} />
        <ProjectHarnessOverrides
          idPrefix="project-edit"
          planner={plannerHarness}
          worker={workerHarness}
          onPlannerChange={setPlannerHarness}
          onWorkerChange={setWorkerHarness}
        />
        {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
          <Button
            type="button"
            size="sm"
            onClick={save}
            disabled={busy || backgroundTooLong(background) || harnessTooLong(plannerHarness) || harnessTooLong(workerHarness)}
          >
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            Save
          </Button>
        </div>
        <ProjectKnowledgeSection projectId={project.project_id} />
        <ProjectSkillsSection projectId={project.project_id} />
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-border bg-card px-5 py-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 space-y-1">
          <h2 className="flex items-center gap-2 text-base font-semibold">
            <Folder className="h-4 w-4 shrink-0 text-muted-foreground" />
            <span className="truncate">{project.name}</span>
          </h2>
          {project.description && (
            <p className="text-sm text-muted-foreground whitespace-pre-wrap">{project.description}</p>
          )}
          <p className="flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
            {project.work_dir ? (
              <span className="font-mono text-foreground/80 break-all">{project.work_dir}</span>
            ) : (
              <span>No default directory</span>
            )}
            <span aria-hidden="true">·</span>
            <span>
              {project.task_count} {project.task_count === 1 ? "task" : "tasks"}
            </span>
          </p>
        </div>
        <div className="shrink-0 flex items-center gap-1">
          {confirmDelete ? (
            <>
              <span className="text-xs text-muted-foreground mr-1">Delete this project?</span>
              <Button type="button" variant="destructive" size="sm" onClick={remove} disabled={busy}>
                {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                Delete
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmDelete(false)}>
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button type="button" variant="ghost" size="sm" onClick={startEdit}>Edit</Button>
              {project.task_count === 0 && (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setError("");
                    setConfirmDelete(true);
                  }}
                  className="text-muted-foreground hover:text-destructive hover:bg-destructive/10"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                  Delete project
                </Button>
              )}
            </>
          )}
        </div>
      </div>
      <div className="mt-3 space-y-1 border-t border-border pt-3">
        <h3 className="text-xs font-medium text-muted-foreground">Background</h3>
        {project.background ? (
          <ClampedText key={project.background} text={project.background} className="font-mono text-xs leading-relaxed text-foreground/80" />
        ) : (
          <p className="text-xs text-muted-foreground">No background</p>
        )}
        {(project.planner_harness_prompt || project.worker_harness_prompt) && (
          <p className="pt-1 text-xs text-muted-foreground">
            Harness overrides:{" "}
            <span className="text-foreground/80">
              {[project.planner_harness_prompt && "Planner", project.worker_harness_prompt && "Worker and Reviewer"]
                .filter(Boolean)
                .join(" · ")}
            </span>{" "}
            (others use the default)
          </p>
        )}
      </div>
      {error && <p role="alert" className="mt-2 text-xs text-destructive">{error}</p>}
    </div>
  );
}

interface TasksPageProps {
  sub?: string;
  /** "project=<id>" filters the list by project. */
  query?: string;
  onNavigate?: (sub: string, query?: string) => void;
}

export function TasksPage({ sub, query, onNavigate }: TasksPageProps) {
  const projectFilter = new URLSearchParams(query || "").get("project") || "";
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
  // The project filter the last finished load was for; anything else is still loading.
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const loading = loadedFor !== projectFilter;
  const [error, setError] = useState("");
  const [filter, setFilter] = useState<StatusFilter>("all");
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [newReq, setNewReq] = useState("");
  const [newWorkDir, setNewWorkDir] = useState("");
  const [newProject, setNewProject] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectDir, setNewProjectDir] = useState("");
  const [newProjectDesc, setNewProjectDesc] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");
  const selectedTaskId = sub || null;
  // The list's filter, so leaving a task's detail returns to the list it was opened from.
  const listQuery = useRef("");
  useEffect(() => {
    if (!sub) listQuery.current = query || "";
  }, [sub, query]);
  const setSelectedTaskId = (id: string | null) =>
    id ? onNavigate?.(id) : onNavigate?.("", listQuery.current);
  const setProjectFilter = (id: string) =>
    onNavigate?.("", id ? `project=${encodeURIComponent(id)}` : "");

  // Drop answers for a filter the page has already left.
  const currentFilter = useRef(projectFilter);
  useEffect(() => {
    currentFilter.current = projectFilter;
  }, [projectFilter]);

  const load = () => {
    const requested = projectFilter;
    fetchTasks(requested || undefined)
      .then((data) => {
        if (requested !== currentFilter.current) return;
        setTasks(Array.isArray(data) ? data : (data as any).tasks || []);
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
  }, [projectFilter]);

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

  const chosenProject = projects?.find((p) => p.project_id === newProject) ?? null;
  const isNewProject = newProject === NEW_PROJECT;
  const projectChosen = isNewProject ? !!newProjectName.trim() : !!chosenProject;
  /** The directory a task without its own work_dir would run in. */
  const defaultWorkDir = isNewProject ? newProjectDir.trim() : chosenProject?.work_dir || "";
  const canCreate = !!newReq.trim() && projectChosen && (!!newWorkDir.trim() || !!defaultWorkDir);

  const resetCreate = () => {
    setShowCreate(false);
    setCreateError("");
    setNewReq("");
    setNewWorkDir("");
    setNewProjectName("");
    setNewProjectDir("");
    setNewProjectDesc("");
  };

  const openCreate = () => {
    setNewProject(selectedProject?.project_id ?? (projects?.length === 0 ? NEW_PROJECT : ""));
    setShowCreate(true);
  };

  const handleCreate = async () => {
    if (!canCreate) return;
    setCreating(true);
    setCreateError("");
    // An empty directory is left out so the project's default applies.
    const base = { requirement: newReq.trim(), work_dir: newWorkDir.trim() || undefined };
    try {
      const result = await createTask(
        isNewProject
          ? {
              ...base,
              project: {
                name: newProjectName.trim(),
                description: newProjectDesc.trim() || undefined,
                work_dir: newProjectDir.trim() || undefined,
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

  // The server filters by project too; this keeps the list right while a new filter loads.
  const projectTasks = projectFilter ? tasks.filter((t) => t.project_id === projectFilter) : tasks;
  const filtered = filter === "all" ? projectTasks : projectTasks.filter((t) => t.status === filter);

  const statusCounts = projectTasks.reduce<Record<string, number>>((acc, t) => {
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
          <p className="text-sm text-muted-foreground mt-1">{projectTasks.length} total tasks</p>
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
        <div className="flex items-center gap-1 flex-wrap">
          {STATUS_FILTERS.map((f) => {
            const count = f === "all" ? projectTasks.length : statusCounts[f] || 0;
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
      </div>

      {selectedProject ? (
        <ProjectBar
          key={selectedProject.project_id}
          project={selectedProject}
          onDeleted={() => setProjectFilter("")}
        />
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
              <p className="text-xs text-muted-foreground">
                Task will be created in <strong>init</strong> status. You can review and edit the configuration before starting.
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
