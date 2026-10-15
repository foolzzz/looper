const API_BASE = "/api";

export interface SystemStatus {
  status: string;
  agents: Record<string, { healthy: boolean; agent_id: string }>;
  channels: Record<string, unknown>;
  uptime: number;
  /** The startup self-check, as `looper doctor` prints it. */
  checks?: SelfCheck[];
}

/** One startup check; "skip" means not checked, with the reason in `detail`. */
export interface SelfCheck {
  id: string;
  label: string;
  status: "ok" | "warn" | "fail" | "skip";
  detail: string;
  fix: string;
}

/** A ready task's status: created (from the dashboard or by the Manager) and never started. */
export const READY_STATUS = "init";
export const READY_LABEL = "Ready — not started";

export interface TaskInfo {
  task_id: string;
  requirement: string;
  goal_id: string;
  status: string;
  /** The status's group, from the server's one mapping. */
  group: TaskGroup;
  phase: string;
  planner_id: string | null;
  correlation_id: string;
  created_at: string;
  completed_at: string | null;
  /** Every task belongs to exactly one project. */
  project_id: string;
  project_name: string;
  work_dir: string;
  /** What the task's Planner, Workers and Reviewers spent so far; null when it could not be read. */
  cost?: TaskCost | null;
  /** The schedule that created the task; null for any other task (and once that schedule is deleted). */
  schedule_id: string | null;
  schedule_name: string | null;
}

/** The agents that work on one task; their runs make up its cost. */
export type TaskRole = "planner" | "worker" | "reviewer";

/** A task's cost: the sum of its runs' own costs, in total and by the role of the agent that ran. */
export interface TaskCost {
  cost_usd: number;
  /** Every run, the running ones included. */
  runs: number;
  /** Runs still going: their cost is not known yet. */
  running: number;
  /** Finished runs that ended without a cost (Claude exited first, or their model has no price). */
  unpriced_runs: number;
  /** Runs whose cost is what a subscription's usage would cost at API prices, not money spent. */
  equivalent_runs?: number;
  /** Runs Looper priced at their provider's prices. */
  provider_price_runs?: number;
  by_role: Record<TaskRole, { cost_usd: number; runs: number }>;
}

/** The most characters a project background may have. */
export const BACKGROUND_MAX_CHARS = 10000;

/** The most characters a harness prompt may have, at any settings layer (also in `SettingLimits`). */
export const HARNESS_MAX_CHARS = 20000;

/** How a project's tasks run; null inherits today's behavior. The Agent Profile (model and effort), harness,
 * environment and the per-run cap are agent settings (`ProjectAgentSettings`). */
export interface ProjectExecution {
  /** The most one task may spend over all its runs. */
  task_budget_usd: number | null;
  /** At the task cap: fail at once (also when null) or after the Planner sums up. */
  task_budget_action: "wrap_up" | "fail" | null;
  /** How often the Planner checks running Workers; 0 = never. */
  supervise_interval_seconds: number | null;
  /** "planner": the Planner decides which steps are reviewed; "all": every step is. */
  review_policy: "planner" | "all" | null;
  /** What a task that completes leaves in the project's knowledge: nothing ("off"), a lessons draft to confirm
   * or discard ("draft"), or a lessons document ("write"). Never null. */
  distill_lessons: "off" | "draft" | "write";
}

/** A group of tasks with an optional default work directory and background. */
export interface Project extends ProjectExecution {
  project_id: string;
  name: string;
  description: string;
  /** Default work directory for new tasks; "" = none. */
  work_dir: string;
  /** Added to the system prompt of the project's Planner, Workers and Reviewers; "" = none. */
  background: string;
  created_at: string;
  updated_at: string;
  task_count: number;
  /** Tasks pending, assigned, running or executing. */
  active_count: number;
  /** Its schedules; a project with any cannot be deleted. */
  schedule_count: number;
}

export interface ChatSendResponse {
  correlation_id: string;
  events_url: string;
}

export interface SSEEvent {
  kind: string;
  payload: Record<string, unknown>;
}

export async function fetchStatus(): Promise<SystemStatus> {
  const res = await fetch(`${API_BASE}/status`);
  if (!res.ok) throw new Error(`status ${res.status}`);
  return res.json();
}

/** What a task's status means at a glance; the server maps each status to one (an unknown one to "other"). */
export type TaskGroup = "in_progress" | "queued" | "ready" | "attention" | "completed" | "cancelled" | "other";

export const TASK_GROUPS: { id: TaskGroup; label: string }[] = [
  { id: "in_progress", label: "In progress" },
  { id: "queued", label: "Queued" },
  { id: "ready", label: "Ready" },
  { id: "attention", label: "Needs attention" },
  { id: "completed", label: "Completed" },
  { id: "cancelled", label: "Cancelled" },
  { id: "other", label: "Other" },
];

export function isTaskGroup(value: string | null): value is TaskGroup {
  return TASK_GROUPS.some((g) => g.id === value);
}

/** A task whose Planner runs or waits for room: it can be stopped, and must be before it is deleted. */
export function isActiveTask(task: Pick<TaskInfo, "group">): boolean {
  return task.group === "in_progress" || task.group === "queued";
}

/** A Planner running now: one per task in progress. */
export interface LivePlanner {
  agent_id: string;
  task_id: string;
  project_id: string | null;
  project_name: string | null;
  requirement: string;
}

/** A Worker or Reviewer run now, running or waiting for room under its project's limit. */
export interface LiveStepRun {
  agent_id: string;
  role: "worker" | "reviewer";
  task_id: string;
  project_id: string | null;
  project_name: string | null;
  /** The Planner's label for the step. */
  step: string;
  instruction: string;
  state: "running" | "waiting";
  /** How long it has been running, or waiting. */
  elapsed_seconds: number;
}

/** How many of one role run (and wait) in a project, against its limit there. */
export interface RoleLoad {
  running: number;
  waiting: number;
  limit: number;
}

/** `GET /api/overview`: what is happening now. */
export interface Overview {
  tasks: Record<TaskGroup, number> & { total: number; other_statuses: Record<string, number> };
  agents: {
    /** null when the server has no Manager to ask. */
    manager: { agent_id: string; state: "busy" | "idle" } | null;
    planners: LivePlanner[];
    workers: LiveStepRun[];
    reviewers: LiveStepRun[];
  };
  /** The projects with a task in progress, queued or ready. */
  projects: {
    project_id: string;
    name: string;
    tasks: { running: number; queued: number; ready: number; limit: number };
    workers: RoleLoad;
    reviewers: RoleLoad;
  }[];
  idle_projects: number;
  /** The runs started since the server's local midnight. */
  spend_today: {
    since: string;
    /** Money: API-key runs at their provider's prices. */
    provider_priced_usd: number;
    /** A runtime's own login at API prices: not money spent. */
    equivalent_usd: number;
    runs: number;
    running: number;
    unpriced_runs: number;
  };
  health: { unhealthy: { name: string; restart_count: number; max_restarts: number }[] };
  schedules: OverviewSchedules;
}

/** One schedule as the overview lists it; each links to #projects/<project_id>/schedules/<schedule_id>. */
interface OverviewScheduleRef {
  schedule_id: string;
  name: string;
  project_id: string;
  project_name: string;
}

/** The overview's schedules: the next fires, the failing ones and today's skipped runs. */
export interface OverviewSchedules {
  total: number;
  enabled: number;
  /** Up to 5, soonest first, enabled schedules only. */
  next: (OverviewScheduleRef & { next_fire_at: string; timezone: string })[];
  /** Failures in a row, or an alert active. */
  failing: (OverviewScheduleRef & {
    timezone: string;
    consecutive_failures: number;
    failures_since: string | null;
    /** Set while the "no success for a while" alert is active: when it started. */
    stale_since: string | null;
    last_success_at: string | null;
    last_status: ScheduleRunStatus | null;
  })[];
  /** Runs since the server's local midnight. */
  skipped_today: Record<"skipped_gate" | "skipped_overlap" | "skipped_missed" | "skipped_budget", number>;
}

export async function fetchOverview(): Promise<Overview> {
  const res = await fetch(`${API_BASE}/overview`);
  if (!res.ok) await failed(res, "overview");
  return res.json();
}

export async function fetchTasks(projectId?: string, group?: TaskGroup, scheduleId?: string): Promise<TaskInfo[]> {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  if (group) params.set("group", group);
  if (scheduleId) params.set("schedule_id", scheduleId);
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/tasks${qs ? `?${qs}` : ""}`);
  if (!res.ok) throw new Error(`tasks ${res.status}`);
  const data = await res.json();
  return data.tasks ?? data;
}

/** Who a chat message goes to: the global Manager or a running task's Planner. */
export type ChatTarget = { kind: "manager" } | { kind: "planner"; task_id: string };

/**
 * An agent on the Chat page: the global Manager, each task's Planner and the
 * Worker/Reviewer of each of its steps, finished ones included.
 */
export interface ChatTargetInfo {
  kind: "manager" | "planner" | "worker" | "reviewer";
  id: string;
  /** Only the Manager and a running task's Planner take messages. */
  can_send: boolean;
  task_id?: string;
  /** Planner: the task's requirement. */
  requirement?: string;
  /** Planner: the task's status; Worker/Reviewer: its latest run's status. */
  status?: string;
  /** Worker/Reviewer: the step it ran (0-based), when known. */
  step_index?: number | null;
  instruction?: string;
  started_at?: string;
}

export async function fetchChatTargets(): Promise<ChatTargetInfo[]> {
  const res = await fetch(`${API_BASE}/chat/targets`);
  if (!res.ok) throw new Error(`chat targets ${res.status}`);
  const data = await res.json();
  return data.targets ?? [];
}

export async function sendChat(
  text: string,
  sessionId?: string,
  messageId?: string,
  target: ChatTarget = { kind: "manager" },
): Promise<ChatSendResponse> {
  const res = await fetch(`${API_BASE}/chat/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, session_id: sessionId, message_id: messageId, target }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `chat send ${res.status}`);
  }
  return res.json();
}

export interface DbChatMessage {
  message_id: string;
  role: string;
  content: string;
  created_at: string;
}

export async function fetchChatMessages(sessionId: string): Promise<DbChatMessage[]> {
  const res = await fetch(`${API_BASE}/chat/messages?session_id=${encodeURIComponent(sessionId)}`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.messages || [];
}

/** How long a dropped chat watch waits before it reconnects. */
const CHAT_WATCH_RETRY_MS = 3000;

/**
 * Follow an open chat for messages that answer none of its own (what the
 * Manager's Claude says on its own): each reaches `onMessage` as it is saved.
 * Reconnects while watched; the returned function stops watching.
 */
export function watchChat(sessionId: string, onMessage: (message: DbChatMessage) => void): () => void {
  let stopped = false;
  let stop = () => {};
  let retry: ReturnType<typeof setTimeout> | undefined;
  const connect = () => {
    stop = streamSSE(
      `${API_BASE}/chat/events?session=${encodeURIComponent(sessionId)}`,
      (event) => {
        if (event.kind === "chat.message") onMessage(event.payload as unknown as DbChatMessage);
      },
      () => {
        if (!stopped) retry = setTimeout(connect, CHAT_WATCH_RETRY_MS);
      },
    );
  };
  connect();
  return () => {
    stopped = true;
    clearTimeout(retry);
    stop();
  };
}

export async function saveChatMessage(params: {
  session_id: string;
  message_id: string;
  role: string;
  content: string;
}): Promise<void> {
  await fetch(`${API_BASE}/chat/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  }).catch(() => {});
}

export async function cancelTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  });
  if (!res.ok) throw new Error(`cancel ${res.status}`);
}

export async function deleteTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/delete`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  });
  if (!res.ok) throw new Error(`delete ${res.status}`);
  notifyProjectsChanged();
}

export async function stopTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  });
  if (!res.ok) throw new Error(`stop ${res.status}`);
}

/** Throw with the server's reason when it gives one. */
async function failed(res: Response, label: string): Promise<never> {
  const data = await res.json().catch(() => ({}));
  throw new Error(data.error || `${label} ${res.status}`);
}

export async function startTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  });
  if (!res.ok) await failed(res, "start");
}

/** Start a ready task; it waits while its project runs as many tasks as its limit. Fails unless the task is ready. */
export async function startReadyTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/start`, { method: "POST" });
  if (!res.ok) await failed(res, "start");
}

/** Raise the project's task spend cap above what a task that failed at it spent, and continue the task. */
export async function continueTaskAtCap(taskId: string, taskBudgetUsd: number): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/continue`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_budget_usd: taskBudgetUsd }),
  });
  if (!res.ok) await failed(res, "continue");
}

export interface NewProjectFields {
  name: string;
  description?: string;
  /** Absolute default work directory; omit or "" for none. */
  work_dir?: string;
  /** At most BACKGROUND_MAX_CHARS characters; omit or "" for none. */
  background?: string;
}

/** A new task names exactly one project: an existing one or one created with it. */
type TaskProjectChoice =
  | { project_id: string; project?: never }
  | { project: NewProjectFields; project_id?: never };

export async function createTask(
  params: {
    requirement: string;
    /** The directory the task runs in (absolute or ~/...); omit to use the project's default. */
    work_dir?: string;
    config?: Record<string, unknown>;
    goal_id?: string;
    /** The Agent Profile (profile_id) of some of the task's roles, over every settings layer. */
    profile_overrides?: Partial<Record<TaskRole, string>>;
  } & TaskProjectChoice,
): Promise<{ task_id: string; status: string; created_at: string; project_id: string }> {
  const res = await fetch(`${API_BASE}/tasks/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!res.ok) await failed(res, "create task");
  const data = await res.json();
  notifyProjectsChanged();
  return data;
}

/** Move a task to another project; allowed in any task status. */
export async function setTaskProject(
  taskId: string,
  projectId: string,
): Promise<{ task_id: string; project_id: string; project_name: string }> {
  const res = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/project`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId }),
  });
  if (!res.ok) await failed(res, "move task");
  const data = await res.json();
  notifyProjectsChanged();
  return data;
}

// --- Projects ---

/**
 * Window event fired after this dashboard creates, edits or deletes a project,
 * or creates, moves or deletes a task, so project lists and counts reload at once.
 */
/** The download of the whole configuration (``looper config export``'s --include-projects and --include-secrets). */
export function configExportUrl({ projects, secrets }: { projects: boolean; secrets: boolean }): string {
  return `${API_BASE}/config/export?projects=${projects ? 1 : 0}&secrets=${secrets ? 1 : 0}`;
}

/** The largest configuration file the dashboard can import (the server's config_imports.UPLOAD_MAX_BYTES). */
export const CONFIG_IMPORT_MAX_BYTES = 256 * 1024 * 1024;

export type ConfigImportAction = "added" | "changed" | "unchanged" | "skipped";

/** What an import does or did, as `looper config import --json` prints it. */
export interface ConfigImportPlan {
  applied: boolean;
  /** Each section the file holds: how many of its entries are added, changed, unchanged and skipped. */
  counts: Record<string, Record<ConfigImportAction, number>>;
  /** Every entry but the unchanged ones: the fields that change, or why it is skipped. */
  changes: { section: string; key: string; action: Exclude<ConfigImportAction, "unchanged">; fields?: string[]; reason?: string }[];
  /** Secrets this machine still needs; after applying, `target` is the stored row whose form takes them. */
  need_secret: { section: string; key: string; names: string[]; target?: { kind: "provider" | "mcp" | "channel"; id: string } }[];
  notes: string[];
}

/** An uploaded file waiting to be applied (or cancelled) until `expires_at`, with its preview. */
export interface ConfigImportUpload {
  import_id: string;
  bytes: number;
  expires_at: string;
  /** The file's own head: format, looper_version, exported_at and secrets ("omitted" or "included"). */
  file: Partial<Record<"format" | "looper_version" | "exported_at" | "secrets", string>>;
  plan: ConfigImportPlan;
}

/** Upload a configuration file (streamed to the server) and preview importing it; nothing is written. */
export async function uploadConfigImport(file: File): Promise<ConfigImportUpload> {
  const res = await fetch(`${API_BASE}/config/imports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: file,
  });
  if (!res.ok) await failed(res, "upload");
  return res.json();
}

/** Write an uploaded file in one transaction, like `looper config import --apply`; the server deletes the file. */
export async function applyConfigImport(importId: string): Promise<{ import_id: string; plan: ConfigImportPlan }> {
  const res = await fetch(`${API_BASE}/config/imports/${encodeURIComponent(importId)}/apply`, { method: "POST" });
  if (!res.ok) await failed(res, "apply");
  const data = await res.json();
  notifyProjectsChanged();
  window.dispatchEvent(new Event(REGISTRY_CHANGED_EVENT));
  return data;
}

/** Drop an uploaded file without applying it. */
export async function cancelConfigImport(importId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/config/imports/${encodeURIComponent(importId)}`, { method: "DELETE" });
  if (!res.ok && res.status !== 404) await failed(res, "cancel");
}

export const PROJECTS_CHANGED_EVENT = "looper:projects-changed";

function notifyProjectsChanged() {
  window.dispatchEvent(new Event(PROJECTS_CHANGED_EVENT));
}

export async function fetchProjects(): Promise<Project[]> {
  const res = await fetch(`${API_BASE}/projects`);
  if (!res.ok) await failed(res, "projects");
  const data = await res.json();
  return data.projects ?? [];
}

export async function createProject(fields: NewProjectFields): Promise<Project> {
  const res = await fetch(`${API_BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "create project");
  const project: Project = await res.json();
  notifyProjectsChanged();
  return project;
}

/** The project fields an update can change; an execution setting set to null inherits again. */
export type ProjectUpdate = Partial<Omit<NewProjectFields, "name">> & { name?: string } & {
  [K in keyof ProjectExecution]?: ProjectExecution[K] | string;
};

/** Send only the fields to change; `work_dir: ""` and `background: ""` clear them. */
export async function updateProject(projectId: string, fields: ProjectUpdate): Promise<Project> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update project");
  const project: Project = await res.json();
  notifyProjectsChanged();
  return project;
}

/** Only an empty project can be deleted; the server answers 409 otherwise. */
export async function deleteProject(projectId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}`, {
    method: "DELETE",
  });
  if (!res.ok) await failed(res, "delete project");
  notifyProjectsChanged();
}

// --- A project's schedules: a full task of the project on a time rule ---

/** "skip": a firing while the previous run's task is in progress is skipped; "queue": it waits (at most one). */
export type ScheduleOverlap = "skip" | "queue";
/** After downtime: "once" runs once for all missed fires; "skip" skips them unless the latest was within 2 minutes. */
export type ScheduleCatchUp = "once" | "skip";
export type ScheduleRunTrigger = "scheduled" | "catch_up" | "manual";
/** Final: all but "queued", "gating" and "running". Failures: "failed", "timed_out" and "gate_failed". */
export type ScheduleRunStatus =
  | "queued"
  | "gating"
  | "running"
  | "completed"
  | "failed"
  | "stopped"
  | "cancelled"
  | "timed_out"
  | "skipped_overlap"
  | "skipped_missed"
  | "skipped_gate"
  | "gate_failed"
  | "skipped_budget";
/** "failures": failed runs and the two alerts with their recoveries; "always": every run that ran a task, and the
 * alerts; "never": nothing is sent. */
export type ScheduleNotify = "failures" | "always" | "never";

/** A chat of a channel the notifications go to. */
export interface ScheduleNotifyTarget {
  channel: string;
  chat: string;
}

/** A chat a channel's sender allowlist (Settings → Channels) allows, as a schedule's notify target. */
export interface ScheduleNotifyTargetOption extends ScheduleNotifyTarget {
  label: string;
  channel_enabled: boolean;
}

/** One firing of a schedule (or a "Run now"), and the task it started. */
export interface ScheduleRunInfo {
  run_id: string;
  schedule_id: string;
  /** The fire time this run is for. */
  scheduled_for: string;
  /** Null while queued. */
  fired_at: string | null;
  trigger: ScheduleRunTrigger;
  /** Fires missed before this run (a catch-up run stands in for them). */
  missed: number;
  status: ScheduleRunStatus;
  /** Null for a skipped run, and while queued. */
  task_id: string | null;
  task_status: string | null;
  /** The task's result or error, set when the run ends. */
  summary: string | null;
  /** The task's cost, live while it runs. */
  cost_usd: number | null;
  /** Why the run failed or was skipped; for "gate_failed", the reason and the tail of the gate's stderr. */
  error: string | null;
  finished_at: string | null;
  /** What the gate command printed (stored truncated); null without a gate. */
  gate_output: string | null;
  /** The run whose unfinished input this run was given. */
  carried_from: string | null;
}

export interface ScheduleInfo {
  schedule_id: string;
  project_id: string;
  name: string;
  /** Five-field cron rule, read in `timezone`. */
  cron: string;
  timezone: string;
  /** The rule in words, e.g. "Weekdays at 09:00 (America/Los_Angeles)". */
  description: string;
  requirement: string;
  /** "" = the project's default directory at firing time. */
  work_dir: string;
  overlap: ScheduleOverlap;
  catch_up: ScheduleCatchUp;
  /** Null: no limit. */
  run_timeout_seconds: number | null;
  /** Null: the project's task spend cap applies. */
  run_budget_usd: number | null;
  /** The Agent Profile of some of a run's roles; {} = none. */
  profile_overrides: Partial<Record<TaskRole, string>>;
  /** A new schedule starts disabled. */
  enabled: boolean;
  /** Null while disabled. */
  next_fire_at: string | null;
  last_fired_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  created_via: "dashboard" | "manager";
  created_at: string;
  updated_at: string;
  /** The newest run of any status. */
  last_run: ScheduleRunInfo | null;
  /** Runs in the work directory before each run; null: no gate. */
  gate_command: string | null;
  /** Null: `limits.gate_timeout_default_seconds`. */
  gate_timeout_seconds: number | null;
  /** Null: no daily cap. */
  daily_budget_usd: number | null;
  notify: ScheduleNotify;
  /** Null: dashboard only. */
  notify_target: ScheduleNotifyTarget | null;
  /** This schedule's runs since the server's local midnight (running ones at their cost so far). */
  spent_today_usd: number;
  /** When each alert became active; null while it is not. */
  alerts: { failures_since: string | null; stale_since: string | null };
}

export interface ScheduleLimits {
  name_max_chars: number;
  requirement_max_chars: number;
  /** Fires closer than this make a rule invalid. */
  min_interval_seconds: number;
  default_timezone: string;
  roles: TaskRole[];
  gate_command_max_chars: number;
  gate_timeout_default_seconds: number;
  gate_timeout_max_seconds: number;
  /** Empty: no channel has a sender to send to. */
  notify_targets: ScheduleNotifyTargetOption[];
}

/** A rule checked by the server: its description and next fire times, or why it is invalid. */
export interface SchedulePreview {
  valid: boolean;
  cron: string;
  timezone: string;
  description: string | null;
  next: string[];
  error: string | null;
}

/** A new schedule's fields; one is always created disabled. */
export interface ScheduleFields {
  name: string;
  cron: string;
  requirement: string;
  timezone?: string;
  work_dir?: string;
  overlap?: ScheduleOverlap;
  catch_up?: ScheduleCatchUp;
  run_timeout_seconds?: number | null;
  run_budget_usd?: number | null;
  profile_overrides?: Partial<Record<TaskRole, string>>;
  /** "" or null clears it. */
  gate_command?: string | null;
  gate_timeout_seconds?: number | null;
  daily_budget_usd?: number | null;
  notify?: ScheduleNotify;
  notify_target?: ScheduleNotifyTarget | null;
}

/** Any of a schedule's fields, plus turning it on or off. */
export type ScheduleUpdate = Partial<ScheduleFields> & { enabled?: boolean };

const scheduleUrl = (scheduleId: string) => `${API_BASE}/schedules/${encodeURIComponent(scheduleId)}`;

/** Send JSON (or nothing) to a schedule endpoint; a refusal throws the server's reason. */
async function scheduleCall<T>(method: string, url: string, label: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) await failed(res, label);
  return res.json();
}

/** A project's schedules, ordered by name, and the limits their fields have. */
export function fetchSchedules(projectId: string): Promise<{ schedules: ScheduleInfo[]; limits: ScheduleLimits }> {
  return scheduleCall("GET", `${API_BASE}/projects/${encodeURIComponent(projectId)}/schedules`, "schedules");
}

/** Create a schedule, disabled; 409 when the project has one with that name already. */
export async function createSchedule(projectId: string, fields: ScheduleFields): Promise<ScheduleInfo> {
  const data = await scheduleCall<{ schedule: ScheduleInfo }>(
    "POST",
    `${API_BASE}/projects/${encodeURIComponent(projectId)}/schedules`,
    "create schedule",
    fields,
  );
  notifyProjectsChanged();
  return data.schedule;
}

export async function fetchSchedule(scheduleId: string): Promise<ScheduleInfo> {
  return (await scheduleCall<{ schedule: ScheduleInfo }>("GET", scheduleUrl(scheduleId), "schedule")).schedule;
}

/** Send only the fields to change; enabling sets the next fire time from now, disabling clears it. */
export async function updateSchedule(scheduleId: string, fields: ScheduleUpdate): Promise<ScheduleInfo> {
  return (await scheduleCall<{ schedule: ScheduleInfo }>("PUT", scheduleUrl(scheduleId), "update schedule", fields))
    .schedule;
}

/** Delete a schedule with its runs; the tasks it created stay. */
export async function deleteSchedule(scheduleId: string): Promise<void> {
  await scheduleCall("DELETE", scheduleUrl(scheduleId), "delete schedule");
  notifyProjectsChanged();
}

/** Run now, also while disabled: the run is running, queued, skipped_overlap, or failed with its error. */
export async function runSchedule(scheduleId: string): Promise<ScheduleRunInfo> {
  return (await scheduleCall<{ run: ScheduleRunInfo }>("POST", `${scheduleUrl(scheduleId)}/run`, "run schedule")).run;
}

/** A schedule's runs, newest first. */
export async function fetchScheduleRuns(scheduleId: string, limit?: number): Promise<ScheduleRunInfo[]> {
  const qs = limit ? `?limit=${limit}` : "";
  return (await scheduleCall<{ runs: ScheduleRunInfo[] }>("GET", `${scheduleUrl(scheduleId)}/runs${qs}`, "schedule runs"))
    .runs;
}

/** A message about a schedule: a run's result, or an alert or its recovery. */
export interface ScheduleNotification {
  notification_id: string;
  kind: "run" | "failures_alert" | "failures_recovered" | "stale_alert" | "stale_recovered";
  run_id: string | null;
  channel: string | null;
  chat: string | null;
  text: string;
  /** "dashboard": it had no target. */
  status: "sent" | "failed" | "dashboard";
  error: string | null;
  created_at: string;
}

/** A schedule's notifications, newest first. */
export async function fetchScheduleNotifications(scheduleId: string, limit?: number): Promise<ScheduleNotification[]> {
  const qs = limit ? `?limit=${limit}` : "";
  return (
    await scheduleCall<{ notifications: ScheduleNotification[] }>(
      "GET",
      `${scheduleUrl(scheduleId)}/notifications${qs}`,
      "schedule notifications",
    )
  ).notifications;
}

/** Check a rule: its description and next `count` fire times (5 by default), or why it is invalid. */
export function previewSchedule(fields: { cron: string; timezone?: string; count?: number }): Promise<SchedulePreview> {
  return scheduleCall("POST", `${API_BASE}/schedules/preview`, "preview", fields);
}

// --- A project's knowledge: documents its Planner, Workers and Reviewers read on demand ---

/** Written here (text), an uploaded text file (upload), or a file in the project's default directory (path). */
export type KnowledgeSourceKind = "text" | "upload" | "path";

export interface KnowledgeDocument {
  document_id: string;
  title: string;
  /** One line; the agents' index shows it beside the title. */
  summary: string;
  source_kind: KnowledgeSourceKind;
  /** The materialized file's name (text and upload); null for a path document. */
  filename: string | null;
  /** A path document's path, relative to the project's default directory; null otherwise. */
  path: string | null;
  /** Where agents read it. */
  read_path: string;
  /** A path document whose file is gone (agents see it listed as missing). */
  missing: boolean;
  size_bytes: number;
  sha256: string;
  created_at: string;
  updated_at: string;
}

export interface ProjectKnowledge {
  project_id: string;
  documents: KnowledgeDocument[];
  count: number;
  /** The text and upload documents' content; path documents are read live and not stored. */
  stored_bytes: number;
  /** The index agents get in their system prompt. */
  index: string;
  index_chars: number;
  caps: {
    document_max_bytes: number;
    project_max_documents: number;
    project_max_bytes: number;
    index_max_chars: number;
  };
}

export type NewKnowledgeDocument = { title: string; summary: string } & (
  | { source_kind: "text"; content: string }
  | { source_kind: "upload"; filename: string; content_base64: string }
  | { source_kind: "path"; path: string }
);

function knowledgeUrl(projectId: string, documentId?: string) {
  const base = `${API_BASE}/projects/${encodeURIComponent(projectId)}/knowledge`;
  return documentId ? `${base}/${encodeURIComponent(documentId)}` : base;
}

export async function fetchKnowledge(projectId: string): Promise<ProjectKnowledge> {
  const res = await fetch(knowledgeUrl(projectId));
  if (!res.ok) await failed(res, "knowledge");
  return res.json();
}

/** Add a document; the server refuses one over a cap with the reason (400). */
export async function addKnowledgeDocument(projectId: string, document: NewKnowledgeDocument): Promise<ProjectKnowledge> {
  const res = await fetch(knowledgeUrl(projectId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(document),
  });
  if (!res.ok) await failed(res, "add document");
  return res.json();
}

/** One document and its body: stored for text and upload, the file as it is now for a path document. */
export interface KnowledgeDocumentBody {
  document: KnowledgeDocument;
  /** null when a path document's file is missing or not text; content_error says why. */
  content: string | null;
  content_error?: string;
}

export async function fetchKnowledgeDocument(projectId: string, documentId: string): Promise<KnowledgeDocumentBody> {
  const res = await fetch(knowledgeUrl(projectId, documentId));
  if (!res.ok) await failed(res, "document");
  return res.json();
}

/** A document's changes: title, summary and its source's fields (its kind stays). */
export type KnowledgeDocumentUpdate = {
  title?: string;
  summary?: string;
  content?: string;
  filename?: string;
  content_base64?: string;
  path?: string;
};

/** Change a document; the server refuses one over a cap with the reason (400). */
export async function updateKnowledgeDocument(
  projectId: string,
  documentId: string,
  fields: KnowledgeDocumentUpdate,
): Promise<ProjectKnowledge> {
  const res = await fetch(knowledgeUrl(projectId, documentId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update document");
  return res.json();
}

export async function deleteKnowledgeDocument(projectId: string, documentId: string): Promise<ProjectKnowledge> {
  const res = await fetch(knowledgeUrl(projectId, documentId), { method: "DELETE" });
  if (!res.ok) await failed(res, "delete document");
  return res.json();
}

/** A text document waiting for the user: the lessons a task's Planner wrote ("draft" mode). Agents do not see it. */
export interface KnowledgeDraft {
  draft_id: string;
  /** The task that wrote it; null once that task is deleted. */
  task_id: string | null;
  title: string;
  summary: string;
  content: string;
  size_bytes: number;
  created_at: string;
}

export interface KnowledgeDrafts {
  project_id: string;
  drafts: KnowledgeDraft[];
  count: number;
}

function draftsUrl(projectId: string, draftId?: string) {
  return `${knowledgeUrl(projectId)}/drafts${draftId ? `/${encodeURIComponent(draftId)}` : ""}`;
}

export async function fetchKnowledgeDrafts(projectId: string): Promise<KnowledgeDrafts> {
  const res = await fetch(draftsUrl(projectId));
  if (!res.ok) await failed(res, "drafts");
  return res.json();
}

/** Make a draft a text document, with any changed title, summary or content; answers with the knowledge. */
export async function confirmKnowledgeDraft(
  projectId: string,
  draftId: string,
  fields: { title?: string; summary?: string; content?: string } = {},
): Promise<ProjectKnowledge> {
  const res = await fetch(`${draftsUrl(projectId, draftId)}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "confirm draft");
  return res.json();
}

/** Remove a draft; answers with the remaining drafts. */
export async function discardKnowledgeDraft(projectId: string, draftId: string): Promise<KnowledgeDrafts> {
  const res = await fetch(draftsUrl(projectId, draftId), { method: "DELETE" });
  if (!res.ok) await failed(res, "discard draft");
  return res.json();
}

// --- A project's own skills: catalog items the project writes, installed in it for its task roles ---

export interface ProjectSkill {
  /** The catalog item's id. */
  skill_id: string;
  /** A lowercase slug; the skill's directory and frontmatter name. */
  name: string;
  /** How agents see it: `project:<name>`. */
  qualified_name: string;
  /** One line; Claude Code shows it to the model to decide when to use the skill. */
  description: string;
  /** The size of its SKILL.md (frontmatter included). */
  size_bytes: number;
  created_at: string;
  updated_at: string;
}

export interface SkillCaps {
  name_max_chars: number;
  description_max_chars: number;
  content_max_bytes: number;
  /** How many skills one project may write. */
  project_max_skills: number;
}

export interface NewProjectSkill {
  name: string;
  description: string;
  /** The SKILL.md body, without frontmatter. */
  content: string;
}

function skillsUrl(projectId: string, skillId?: string) {
  const base = `${API_BASE}/projects/${encodeURIComponent(projectId)}/skills`;
  return skillId ? `${base}/${encodeURIComponent(skillId)}` : base;
}

/**
 * Write a skill in the project: a catalog item, installed in the project for the Planner, Workers and Reviewers.
 * The server refuses a bad field or a cap with the reason (400) and a taken name (409). Answers with the project's
 * view of its installs.
 */
export async function addProjectSkill(
  projectId: string,
  skill: NewProjectSkill,
): Promise<ProjectInstalls & { skill: ProjectSkill }> {
  const res = await fetch(skillsUrl(projectId), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(skill),
  });
  if (!res.ok) await failed(res, "add skill");
  return res.json();
}

/** One skill the project wrote and its SKILL.md body. */
export interface ProjectSkillBody {
  skill: ProjectSkill;
  content: string;
}

export async function fetchProjectSkill(projectId: string, skillId: string): Promise<ProjectSkillBody> {
  const res = await fetch(skillsUrl(projectId, skillId));
  if (!res.ok) await failed(res, "skill");
  return res.json();
}

/** Change a skill's name, description or body; a bad field is 400, a taken name 409. Switch it with its install. */
export async function updateProjectSkill(
  projectId: string,
  skillId: string,
  fields: Partial<NewProjectSkill>,
): Promise<ProjectInstalls & { skill: ProjectSkill }> {
  const res = await fetch(skillsUrl(projectId, skillId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update skill");
  return res.json();
}

/** Delete a skill the project wrote: from the catalog and from every project it is installed in. */
export async function deleteProjectSkill(
  projectId: string,
  skillId: string,
): Promise<ProjectInstalls & { deleted: string }> {
  const res = await fetch(skillsUrl(projectId, skillId), { method: "DELETE" });
  if (!res.ok) await failed(res, "delete skill");
  return res.json();
}

export async function updateTaskConfig(
  taskId: string,
  fields: { requirement?: string; work_dir?: string; config?: Record<string, unknown> },
): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/${taskId}/config`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update task config");
}

export interface TaskDetail extends TaskInfo {
  work_dir: string;
  config: Record<string, unknown>;
  origin_channel_id: string;
  result_summary: string | null;
  error: string | null;
  /** "task_budget": the task failed at its project's task spend cap and can continue with a higher one. */
  fail_kind: "task_budget" | null;
  /** The task's spend cap (USD): its own, else its project's per task; null for none. */
  task_budget_usd: number | null;
  /** The task's own cap (a scheduled run's), raised alone when it continues at its cap; null for none. */
  own_budget_usd: number | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  steps: TaskStep[];
  /** The task journal, oldest first: what happened in the task that later agents and sessions need. */
  journal?: JournalEntry[];
  /** The Planner's progress notes, newest first (absent before the API gave them). */
  progress?: ProgressNote[];
  /** The Agent Profile the task chose for some of its roles, over every settings layer; {} when none. */
  profile_overrides?: Partial<Record<TaskRole, AgentProfile>>;
}

/** What a task journal entry records; code writes each when its event happens. */
export type JournalKind =
  | "step_accepted"
  | "step_failed"
  | "step_retry"
  | "decision"
  | "user_instruction"
  | "session_switch"
  | "handoff"
  | "progress";

/** Where a task stood, as its Planner said with a decision or a periodic check. */
export interface ProgressNote {
  done: string;
  now: string;
  next: string;
  risks: string;
  estimate: string;
  /** When the note was kept. */
  at: string;
}

/** One event of a task that later agents and sessions need: a step's outcome with its agents' learnings, a
 * Planner decision, a user instruction, a new Planner session or the handoff before it. */
export interface JournalEntry {
  id: number;
  /** "" for an entry of the whole task. */
  step_id: string;
  /** The Planner's label for the step; "" for an entry of the whole task or when the step is gone. */
  step_key: string;
  kind: JournalKind;
  summary: string;
  learnings: string[];
  created_at: string;
}

export interface TaskStep {
  id: string;
  task_id: string;
  step_index: number;
  instruction: string;
  target_files: string[];
  status: string;
  worker_id: string | null;
  result: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface ActivityEvent {
  seq: number;
  task_id: string;
  worker: string;
  kind: string;
  summary: string | null;
  detail?: string | null;
  metadata: Record<string, unknown> | null;
  input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
}

export async function fetchTaskDetail(taskId: string): Promise<TaskDetail> {
  const res = await fetch(`${API_BASE}/tasks/${taskId}`);
  if (!res.ok) throw new Error(`task detail ${res.status}`);
  return res.json();
}

export async function fetchTaskActivity(
  taskId: string,
  since = 0,
  limit = 100,
): Promise<{ activity: ActivityEvent[] }> {
  const res = await fetch(
    `${API_BASE}/tasks/${taskId}/activity?since=${since}&limit=${limit}`,
  );
  if (!res.ok) throw new Error(`activity ${res.status}`);
  return res.json();
}

export async function fetchTaskConversation(
  taskId: string,
): Promise<{ conversation: ActivityEvent[] }> {
  const res = await fetch(`${API_BASE}/tasks/${taskId}/conversation`);
  if (!res.ok) throw new Error(`conversation ${res.status}`);
  return res.json();
}

export function streamTaskActivity(
  taskId: string,
  since: number,
  onEvent: (event: ActivityEvent) => void,
  onDone?: () => void,
): () => void {
  const abortController = new AbortController();

  (async () => {
    try {
      const res = await fetch(
        `${API_BASE}/tasks/${taskId}/activity/stream?since=${since}`,
        { signal: abortController.signal },
      );
      if (!res.ok || !res.body) return;

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let eventData = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            eventData = line.slice(6);
          } else if (line === "" && eventData) {
            try {
              const parsed = JSON.parse(eventData);
              onEvent(parsed);
            } catch {
              // skip
            }
            eventData = "";
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        console.error("Activity SSE error:", e);
      }
    } finally {
      onDone?.();
    }
  })();

  return () => abortController.abort();
}

// --- Role configs: each role's own settings ---

export interface RoleConfig {
  role: string;
  display_name: string;
  enabled: boolean;
  /** The role prompt: what this kind of agent is responsible for. */
  system_prompt: string;
  /** The system-recommended prompt; "Reset to recommended" copies it into system_prompt. */
  default_prompt: string;
  max_turns: number;
  timeout_seconds: number;
  extra_args: Record<string, unknown> | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

/** Where a run's harness came from: a settings layer, or (runs recorded before the layers) the old source names. */
export type HarnessSource =
  | SettingLayer
  | "builtin_default"
  | "system_default"
  | "manager_override"
  | "project_override";
/** Where a run's model came from: a settings layer, or (older runs) the old source names. */
export type ModelSource =
  | RunSettingSource
  | "builtin_default"
  | "system_default"
  | "agent_override"
  | "project_override";
/** Where a run's setting came from: runs recorded before Agent Profiles were named say "agent" for "profile". */
export type RunSettingSource = SettingSource | "agent";
/** The Agent Profile a run used and where it was selected. */
export interface RunProfile {
  id: string;
  name: string;
  model: string;
  runtime: { id: string; name: string; type: string };
  provider: { id: string; name: string; auth: ProviderAuth };
  source: RunSettingSource;
}

/** Where each layer of a run's system prompt came from (absent on runs recorded before it existed). */
export interface PromptLayers {
  /** from_role "worker": a Reviewer without a harness of its own at that layer ran with the Worker's. */
  harness?: { source: HarnessSource; sha256: string; project_id?: string; from_role?: "worker" } | null;
  role?: { config_id: string; sha256: string; recommended: boolean };
  project?: { id: string; background_sha256: string | null; updated_at: string | null } | null;
  task?: { kind: "plan" | "work" | "review"; task_id: string | null; step_id: string | null; sha256: string } | null;
  contract?: { name: string; sha256: string };
  /** The flag set that kept the operator's personal Claude Code configuration out of the run. */
  isolation?: string;
}

/** One of the repository's instruction files a run's runtime was given: read by the runtime itself, or put in its
 * system prompt by Looper (the text's sha256, the file's size and the bytes clipped; or why it could not be read). */
export interface ProjectInstructionFile {
  /** From the run's working directory. */
  path: string;
  delivery: "native" | "injected";
  sha256?: string;
  bytes?: number;
  clipped_bytes?: number;
  error?: string;
}

export interface LlmRecord {
  id: number;
  agent_id: string;
  task_id: string | null;
  session_id: string | null;
  status: "running" | "completed" | "failed";
  input_summary: string | null;
  output_summary: string | null;
  input_text: string | null;
  output_text: string | null;
  /** The run's own tokens; for a resident agent's turn, that turn's share of its session. */
  input_tokens: number | null;
  output_tokens: number | null;
  /** Prompt tokens read from the cache and written to it. */
  cache_read_tokens?: number | null;
  cache_creation_tokens?: number | null;
  /** The run's own cost; for a resident agent's turn, that turn's share of its session. */
  cost_usd: number | null;
  /** Who priced the run: the runtime reported it, or Looper from the provider's prices; null when unpriced. */
  cost_source?: "runtime" | "provider_price" | null;
  /** The context at the run's end: the prompt of its last main-loop model request. */
  context_tokens?: number | null;
  /** The context compactions during the run; null for runs recorded before they were. */
  compactions?: Compaction[] | null;
  duration_ms: number | null;
  model: string | null;
  error: string | null;
  metadata: {
    command?: string[];
    cwd?: string;
    /** What Looper appended to Claude Code's system prompt (its built-in prompt is not included). */
    system_prompt?: string;
    system_prompt_sha256?: string;
    prompt_layers?: PromptLayers;
    /** The Agent Profile the run used and where it was selected (`runProfile` reads it for every run). */
    profile?: RunProfile;
    /** Runs recorded before Agent Profiles were named: the profile under its old name (runs before Agent
     * Profiles lack both). */
    agent?: RunProfile;
    /** The model asked for (an alias or full name) and where it came from. */
    model_choice?: { model: string; source: ModelSource; effort: string | null };
    /** The environment variables from the agent settings: their names, with each value's sha256. */
    env?: { sha256: string; keys: Record<string, string> };
    /** The settings layer each setting of the run came from (runs recorded before the layers lack it). */
    settings_sources?: {
      harness: SettingLayer;
      profile?: RunSettingSource;
      /** Runs recorded before Agent Profiles were named name the profile's layer `agent`; runs before Agent
       * Profiles name the model's layer instead. */
      agent?: RunSettingSource;
      model?: RunSettingSource;
      effort: RunSettingSource;
      run_budget_usd: SettingLayer;
      env: Record<string, SettingLayer>;
    };
    /** The repository's own CLAUDE.md and AGENTS.md the runtime was given (runs recorded before it lack it). */
    project_instructions?: ProjectInstructionFile[];
    /** The catalog items installed for the run (plugins and skills) and their hash; error when it went without
     * them. */
    library?: RunLibrary;
    /** Runs recorded before the catalog: the project skills a task agent loaded. */
    project_skills?: RunProjectSkills;
    /** Not a Claude run: the user sent /new and the Manager dropped this session (sender: Lark open_id or
     * Telegram user id; none from the web chat). */
    session_reset?: { channel: string; sender: string | null; chat: string | null; previous_session_id: string | null };
  } | null;
  created_at: string;
  finished_at: string | null;
  /** Heartbeat: last time the run produced any output. */
  last_output_at: string | null;
}

/** The Agent Profile a run used: `metadata.profile`, or `metadata.agent` on a run recorded before Agent Profiles were
 * named; null for a run recorded before Agent Profiles. */
export function runProfile(record: Pick<LlmRecord, "metadata">): RunProfile | null {
  return record.metadata?.profile ?? record.metadata?.agent ?? null;
}

/** One context compaction (Claude Code's compact_boundary): automatic or /compact, with its context before and after. */
export interface Compaction {
  trigger?: "auto" | "manual" | string;
  pre_tokens?: number;
  post_tokens?: number;
  duration_ms?: number;
}

/** One of an agent's turns, for its context over time. */
export interface AgentTurn {
  id: number;
  created_at: string;
  status: LlmRecord["status"];
  session_id: string | null;
  cost_usd: number | null;
  context_tokens: number | null;
  compactions: Compaction[] | null;
}

/** An agent's spending in total, by session and by UTC day, and its latest turns oldest first. */
export interface AgentUsage {
  agent_id: string;
  total: { cost_usd: number; runs: number; running: number; unpriced_runs: number; compactions: number };
  by_session: { session_id: string | null; cost_usd: number; runs: number; first_at: string; last_at: string }[];
  by_day: { day: string; cost_usd: number; runs: number }[];
  turns: AgentTurn[];
  latest_context_tokens: number | null;
}

export async function fetchAgentUsage(agentId: string, turns = 100): Promise<AgentUsage> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/usage?turns=${turns}`);
  if (!res.ok) throw new Error(`agent usage ${res.status}`);
  return res.json();
}

/** One stream-json event of a run, in order. */
export interface LlmRecordEvent {
  seq: number;
  event: Record<string, unknown>;
  created_at: string;
}

export async function fetchRoleConfigs(): Promise<RoleConfig[]> {
  const res = await fetch(`${API_BASE}/roles`);
  if (!res.ok) throw new Error(`roles ${res.status}`);
  const data = await res.json();
  return data.configs ?? [];
}

export async function fetchRoleConfig(role: string): Promise<RoleConfig> {
  const res = await fetch(`${API_BASE}/roles/${role}`);
  if (!res.ok) throw new Error(`role ${res.status}`);
  return res.json();
}

export async function updateRoleConfig(
  role: string,
  fields: Partial<RoleConfig>,
): Promise<RoleConfig> {
  const res = await fetch(`${API_BASE}/roles/${role}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update role");
  return res.json();
}

/** Reset a role's prompt to the system-recommended default. */
export async function resetRolePrompt(role: string): Promise<RoleConfig> {
  const res = await fetch(`${API_BASE}/roles/${role}/reset-prompt`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`reset role prompt ${res.status}`);
  return res.json();
}

export async function fetchLlmRecords(params: {
  agent_id?: string;
  task_id?: string;
  /** A role: "manager", "planner" (each task's Planner), "worker" or "reviewer". */
  role?: string;
  limit?: number;
  offset?: number;
}): Promise<LlmRecord[]> {
  const qs = new URLSearchParams();
  if (params.agent_id) qs.set("agent_id", params.agent_id);
  if (params.role) qs.set("role", params.role);
  if (params.task_id) qs.set("task_id", params.task_id);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  const res = await fetch(`${API_BASE}/llm-records?${qs}`);
  if (!res.ok) throw new Error(`llm-records ${res.status}`);
  const data = await res.json();
  return data.records ?? [];
}

export async function fetchLlmRecord(recordId: number): Promise<LlmRecord> {
  const res = await fetch(`${API_BASE}/llm-records/${recordId}`);
  if (!res.ok) throw new Error(`llm-record ${res.status}`);
  return (await res.json()).record;
}

/** Events with seq greater than `after`, so a viewer can follow a running agent. */
export async function fetchLlmRecordEvents(recordId: number, after = 0): Promise<LlmRecordEvent[]> {
  const res = await fetch(`${API_BASE}/llm-records/${recordId}/events?after=${after}`);
  if (!res.ok) throw new Error(`llm-record events ${res.status}`);
  return (await res.json()).events ?? [];
}

// --- The registry: runtimes, LLM providers and the Agent Profiles made of them ---

/** What a runtime of one type can do; `capabilities` decide which roles its Agent Profiles can run. */
export interface RuntimeType {
  type: string;
  label: string;
  /** The command a runtime of this type runs when its executable is empty. */
  default_executable: string;
  /** The id its built-in runtime has once found on the server's PATH: rt-<type>. */
  runtime_id: string;
  protocols: string[];
  capabilities: string[];
  /** The roles its capabilities let it run. */
  roles: AgentRole[];
  auth_kinds: ProviderAuth[];
}

/** A runtime on this machine: built in (one per type found on the server's PATH), or one registered by hand
 * before runtimes were built in (`builtin` false), kept read-only while an Agent Profile uses it. */
export interface Runtime {
  runtime_id: string;
  name: string;
  type: string;
  type_label: string;
  /** "" = the type's default (for the built-in Claude Code runtime, the server's claude). */
  executable: string;
  default_executable: string | null;
  /** Where the executable is on the server's PATH; null when it is not found. */
  path: string | null;
  protocols: string[];
  /** What `<executable> --version` printed at the last probe; null when not probed or it failed. */
  version: string | null;
  capabilities: string[];
  /** The roles its capabilities let it run. */
  roles: AgentRole[];
  probed_at: string | null;
  probe_error: string | null;
  builtin: boolean;
  profile_count: number;
}

/** "oauth": the runtime's own login; "api_key": a key stored in Looper. */
export type ProviderAuth = "oauth" | "api_key";

/** One model a provider serves; every field but the name may be null (unknown). Prices are USD per million tokens. */
export interface ProviderModel {
  name: string;
  context_window: number | null;
  input_usd_per_mtok: number | null;
  output_usd_per_mtok: number | null;
  cache_read_usd_per_mtok: number | null;
  cache_write_usd_per_mtok: number | null;
  efforts: string[] | null;
  /** The model the runtime's side requests use with this one (titles, summaries, quick subagents); null: itself. */
  background_model: string | null;
}

export type PriceField = Exclude<keyof ProviderModel, "name" | "context_window" | "efforts" | "background_model">;

/** An environment variable a provider adds to the runs that use it. */
export interface ProviderEnvEntry {
  key: string;
  value: string;
}

/** A runtime's own login as the server last read it: "chatgpt" (Codex's ChatGPT login, what its login provider
 * runs with), "other_method", "not_logged_in", "unknown", or "unavailable" while a forked login a run found is
 * not cleared. */
export interface ProviderLogin {
  state: "chatgpt" | "other_method" | "not_logged_in" | "unknown" | "unavailable";
  detail: string;
}

/** A registered LLM provider. The key itself is never sent: `api_key` is the mask when one is stored. */
export interface Provider {
  provider_id: string;
  name: string;
  protocols: string[];
  auth: ProviderAuth;
  /** The runtime type whose login an OAuth provider is; null for an API-key provider. */
  oauth_runtime_type: string | null;
  /** "" = the protocol's own endpoint. */
  base_url: string;
  /** Empty: any model. */
  models: ProviderModel[];
  /** The models its runtime's own login offers, to suggest (an OAuth provider; any model is taken). */
  suggested_models: string[];
  /** The login's state, for an OAuth provider whose runtime reports one (Codex); null otherwise. */
  login: ProviderLogin | null;
  /** Its own environment variables for the runs that use it. */
  env: ProviderEnvEntry[];
  /** null = no limit. */
  max_concurrency: number | null;
  api_key: string | null;
  builtin: boolean;
  profile_count: number;
}

/** A known vendor's provider, filled in except for the key: its APIs (each one provider), models and env. */
export interface ProviderPreset {
  preset_id: string;
  label: string;
  name: string;
  apis: { protocols: string[]; base_url: string }[];
  models: Pick<ProviderModel, "name" | "context_window" | "background_model">[];
  env: ProviderEnvEntry[];
  docs: string[];
  note: string;
}

/** How a profile's runs are priced: by its runtime, or by Looper at the provider's prices; which models lack a
 * price. */
export interface ProfileCostBasis {
  source: "runtime" | "provider_price" | null;
  /** The figure is what a subscription's usage would cost at API prices, not money spent. */
  equivalent: boolean;
  /** Models its runs use without a full price: those runs record tokens only and no spend cap applies. */
  unpriced: string[];
}

/** Where a profile is selected: the settings layers (a project's with its name) and how many tasks chose it. */
export interface ProfileUsedBy {
  layers: { scope: SettingLayer; role: AgentRole | null; project_id: string | null; project_name: string | null }[];
  task_count: number;
}

/** An Agent Profile: a runtime, a provider and a model, with an optional effort, which its runs use. */
export interface AgentProfile {
  profile_id: string;
  name: string;
  model: string;
  /** null = the runtime's default. */
  effort: string | null;
  /** Claude Code's agent teams in its runs (Claude Code runtimes only). */
  agent_teams: boolean;
  runtime_id: string;
  runtime_name: string;
  runtime_type: string;
  provider_id: string;
  provider_name: string;
  builtin: boolean;
  /** The roles its runtime can run. */
  roles: AgentRole[];
  used_by: ProfileUsedBy;
  cost: ProfileCostBasis;
}

export interface RegistryLimits {
  name_max_chars: number;
  models_max: number;
  price_fields: PriceField[];
  protocols: string[];
  auth_kinds: ProviderAuth[];
  effort_levels: string[];
  /** The runtime capabilities each role needs. */
  role_capabilities: Record<AgentRole, string[]>;
  /** What a stored secret reads as; sent back, it keeps the secret. */
  secret_mask: string;
  /** Variables a provider's env cannot set: the runtimes' bindings set them. */
  provider_env_owned: string[];
}

export interface Registry {
  runtime_types: RuntimeType[];
  runtimes: Runtime[];
  providers: Provider[];
  profiles: AgentProfile[];
  provider_presets: ProviderPreset[];
  /** The built-in price table: a catalog price left unknown comes from it. */
  prices: { as_of: string; sources: string[]; models: Record<string, Record<PriceField, number | null>> };
  limits: RegistryLimits;
}

/** The provider fields a create or update sends; `api_key` the mask keeps the stored key, null removes it. */
export interface ProviderFields {
  name?: string;
  protocols?: string[];
  auth?: ProviderAuth;
  oauth_runtime_type?: string | null;
  base_url?: string;
  /** A number field may go as typed, so the server's 400 explains it. */
  models?: Record<string, unknown>[];
  env?: ProviderEnvEntry[];
  max_concurrency?: number | string | null;
  api_key?: string | null;
}

export interface ProfileFields {
  name?: string;
  runtime_id?: string;
  provider_id?: string;
  model?: string;
  effort?: string | null;
  agent_teams?: boolean;
}

/** Window event fired after this dashboard changes the registry, so profile selectors reload at once. */
export const REGISTRY_CHANGED_EVENT = "looper:registry-changed";

async function registryCall<T>(method: string, path: string, body: unknown, label: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) await failed(res, label);
  const data = await res.json();
  window.dispatchEvent(new Event(REGISTRY_CHANGED_EVENT));
  return data;
}

export async function fetchRegistry(): Promise<Registry> {
  const res = await fetch(`${API_BASE}/registry`);
  if (!res.ok) await failed(res, "registry");
  return res.json();
}

/** Re-detect: run `<executable> --version` again and record its version or error; for a type's built-in runtime
 * not found yet (rt-<type>), look for it on the server's PATH again (404 when it is not there). */
export function probeRuntime(runtimeId: string): Promise<{ runtime: Runtime }> {
  return registryCall("POST", `/runtimes/${encodeURIComponent(runtimeId)}/probe`, undefined, "re-detect runtime");
}

export function createProvider(fields: ProviderFields): Promise<{ provider: Provider }> {
  return registryCall("POST", "/providers", fields, "add provider");
}

export function updateProvider(providerId: string, fields: ProviderFields): Promise<{ provider: Provider }> {
  return registryCall("PUT", `/providers/${encodeURIComponent(providerId)}`, fields, "update provider");
}

export function deleteProvider(providerId: string): Promise<{ deleted: string }> {
  return registryCall("DELETE", `/providers/${encodeURIComponent(providerId)}`, undefined, "delete provider");
}

/** After checking `codex login status` on the machine: let runs use the Codex login again after a run found it
 * forked. */
export function clearLoginProblem(providerId: string): Promise<{ provider: Provider }> {
  return registryCall("DELETE", `/providers/${encodeURIComponent(providerId)}/login-problem`, undefined,
    "use the login again");
}

/** Register an Agent Profile; refused (400) when its runtime, provider and model could not run together. */
export function createAgentProfile(fields: ProfileFields): Promise<{ profile: AgentProfile; warnings: string[] }> {
  return registryCall("POST", "/agent-profiles", fields, "add Agent Profile");
}

export function updateAgentProfile(
  profileId: string,
  fields: ProfileFields,
): Promise<{ profile: AgentProfile; warnings: string[] }> {
  return registryCall("PUT", `/agent-profiles/${encodeURIComponent(profileId)}`, fields, "update Agent Profile");
}

export function deleteAgentProfile(profileId: string): Promise<{ deleted: string }> {
  return registryCall("DELETE", `/agent-profiles/${encodeURIComponent(profileId)}`, undefined,
    "delete Agent Profile");
}

// --- Agent settings: harness, Agent Profile (with its effort), environment and per-run cap, by layer ---

/** Where a setting comes from, from least to most specific: the built-in default, every role and a role (both in
 * Settings → Roles), a project (all its agents), a role in a project. A later layer wins; the Manager has only the
 * first three. */
export type SettingLayer = "builtin" | "system" | "role" | "project" | "role_project";
/** Where a value came from: a layer, the selected profile itself (its effort), or a task's choice of profile. */
export type SettingSource = SettingLayer | "profile" | "task";
/** The roles a project's agents have: the Manager spans every project. */
export type ProjectRole = Exclude<AgentRole, "manager">;

/** An environment variable a layer sets; value null unsets a variable an earlier layer set (not in Settings). */
export interface LayerEnvEntry {
  key: string;
  value: string | null;
}

/** One layer's own values; null is not set here (inherits). */
export interface LayerValues {
  harness_prompt: string | null;
  /** An Agent Profile's profile_id. */
  profile_id: string | null;
  env: LayerEnvEntry[];
  /** `--max-budget-usd` on each Claude Code process. */
  run_budget_usd: number | null;
  /** How many of the role run at once in one project (the Planner: its tasks); only a Planner, Worker or Reviewer
   * role layer, or that role's layer in a project, sets it. */
  max_concurrency: number | null;
}

export interface Sourced<T> {
  value: T;
  source: SettingSource;
}

/** The Agent Profile that applies, as the layered settings show it. */
export interface EffectiveProfile {
  profile_id: string;
  name: string;
  model: string;
  /** The profile's effort; null = the runtime's default. */
  effort: string | null;
  /** The runtime's and the provider's names. */
  runtime: string;
  provider: string;
}

/** What applies to an agent, and the layer each value came from. */
export interface EffectiveSettings {
  /** from_role "worker": a Reviewer without its own harness at that layer uses the Worker's. */
  harness_prompt: Sourced<string> & { from_role?: "worker" };
  profile: Sourced<EffectiveProfile>;
  /** The profile's own effort: source "profile"; null = the runtime's default (source "builtin"). */
  effort: Sourced<string | null>;
  env: ({ key: string } & Sourced<string>)[];
  /** null = no cap. */
  run_budget_usd: Sourced<number | null>;
  /** The Planner's, Workers' and Reviewers' limit per project; absent for the Manager and the system layer. */
  max_concurrency?: Sourced<number>;
}

export interface SettingLimits {
  harness_max_chars: number;
  env_max_entries: number;
  builtin_harness_prompt: string;
  builtin_model: string;
  /** The Agent Profile every role runs with when no layer selects one. */
  builtin_profile: { profile_id: string; name: string };
  budget_max_usd: number;
  concurrency_max: number;
}

export interface LayerState {
  values: LayerValues;
  effective: EffectiveSettings;
}

/** Settings (the system layer) and each role's layer, with what applies to each role outside any project. */
export interface AgentSettings {
  limits: SettingLimits;
  system: LayerState;
  roles: Record<AgentRole, LayerState>;
}

/** A project's layer and its roles' layers, with what applies to each role in the project. */
export interface ProjectAgentSettings {
  project_id: string;
  limits: SettingLimits;
  project: { values: LayerValues };
  roles: Record<ProjectRole, LayerState>;
}

/** The fields to change; null (or "") inherits again, `env` replaces the layer's whole list. A number field may be
 * sent as typed, so the server's 400 explains it. */
export type LayerUpdate = { [K in keyof LayerValues]?: LayerValues[K] | string };

async function putJson<T>(path: string, body: unknown, label: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) await failed(res, label);
  return res.json();
}

export async function fetchAgentSettings(): Promise<AgentSettings> {
  const res = await fetch(`${API_BASE}/agent-settings`);
  if (!res.ok) await failed(res, "agent settings");
  return res.json();
}

/** The system layer: what every agent inherits (Settings). */
export function updateSystemSettings(fields: LayerUpdate): Promise<AgentSettings> {
  return putJson("/agent-settings/system", fields, "update agent settings");
}

/** A role's layer (Settings → Roles). */
export function updateRoleSettings(role: AgentRole, fields: LayerUpdate): Promise<AgentSettings> {
  return putJson(`/agent-settings/roles/${encodeURIComponent(role)}`, fields, "update agent settings");
}

export async function fetchProjectAgentSettings(projectId: string): Promise<ProjectAgentSettings> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/agent-settings`);
  if (!res.ok) await failed(res, "project agent settings");
  return res.json();
}

/** A project's layer: all its agents. */
export function updateProjectLayer(projectId: string, fields: LayerUpdate): Promise<ProjectAgentSettings> {
  return putJson(`/projects/${encodeURIComponent(projectId)}/agent-settings`, fields, "update project agent settings");
}

/** One role's layer in a project. */
export function updateProjectRoleSettings(
  projectId: string,
  role: ProjectRole,
  fields: LayerUpdate,
): Promise<ProjectAgentSettings> {
  return putJson(
    `/projects/${encodeURIComponent(projectId)}/agent-settings/roles/${encodeURIComponent(role)}`,
    fields,
    "update project agent settings",
  );
}

// --- The capability catalog: items acquired into Looper, and where they are installed ---

export type LibraryKind = "plugin" | "skill" | "mcp";
/** How an item was acquired: imported from this machine, uploaded, taken from git, written in Settings ("ui": a skill
 * or an MCP server) or in a project. */
export type ItemOrigin = "machine" | "upload" | "git" | "ui" | "project";
export type AgentRole = "manager" | "planner" | "worker" | "reviewer";
export type InstallScope = "global" | "project";

/** An item installed on a scope for some roles; a project install never names the manager. */
export interface Install {
  install_id: string;
  item_id: string;
  scope: InstallScope;
  project_id: string | null;
  project_name: string | null;
  roles: AgentRole[];
  /** A disabled install loads nowhere, from the next run on. */
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface CatalogItem {
  item_id: string;
  kind: LibraryKind;
  /** A plugin's name@marketplace, or a skill's name. */
  name: string;
  /** How agents see it: a plugin by its name, a skill as personal:<name> or, written in a project, project:<name>. */
  qualified_name: string;
  origin: ItemOrigin;
  /** The project that wrote it (origin "project"). */
  owner_project_id: string | null;
  owner_project_name: string | null;
  description: string;
  source: string;
  /** A plugin's declared version; "" otherwise. */
  version: string;
  sha256: string;
  size_bytes: number;
  file_count: number;
  /** An uploaded or git item edited here since it came in; re-importing it from git replaces the edit. */
  modified: boolean;
  imported_at: string;
  updated_at: string;
  /** An MCP server's configuration, its env and header values masked; null for a plugin or a skill. */
  mcp: McpConfig | null;
  /** Where it is installed; empty when it loads nowhere. */
  installs: Install[];
}

export type McpTransport = "stdio" | "http" | "sse";

/** How Claude Code starts (stdio) or reaches (http, sse) an MCP server. In a response every env and header value is
 * the mask; sending the mask back for a name keeps its stored value. */
export interface McpConfig {
  transport: McpTransport;
  command: string;
  args: string[];
  env: Record<string, string>;
  url: string;
  headers: Record<string, string>;
}

export interface McpImport {
  imported_at: string;
  source: string;
  servers: number;
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: number;
  skipped: { item: string; path: string; reason: string }[];
  skipped_count: number;
}

export interface LibrarySkipped {
  item: string;
  /** "" when the whole item was left out. */
  path: string;
  reason: string;
  size_bytes: number | null;
}

export interface LibraryImport {
  imported_at: string;
  source: string;
  plugins: number;
  skills: number;
  added: string[];
  updated: string[];
  removed: string[];
  unchanged: number;
  skipped: LibrarySkipped[];
  skipped_count: number;
}

export interface Catalog {
  items: CatalogItem[];
  /** Items installed somewhere. */
  installed_count: number;
  total_bytes: number;
  last_import: LibraryImport | null;
  last_mcp_import: McpImport | null;
  roles: AgentRole[];
  task_roles: AgentRole[];
  caps: {
    file_max_bytes: number;
    item_max_bytes: number;
    item_max_files: number;
    library_max_bytes: number;
    /** The largest SKILL.md or zip an upload takes. */
    upload_max_bytes: number;
  };
  mcp_caps: { servers_max: number; variables_max: number; value_max: number; args_max: number; mask: string };
}

/** One item with its files (no content) and, for a skill, its SKILL.md. */
export interface CatalogItemDetail {
  item: CatalogItem;
  files: { path: string; size_bytes: number; executable: boolean }[];
  skill_md: string | null;
  /** A skill's SKILL.md body, below its frontmatter. */
  content: string | null;
}

/** An install that applies to a project, with its item; inherited ones are global (changed in Settings). */
export interface ProjectInstall extends Install {
  inherited: boolean;
  item: CatalogItem;
}

export interface ProjectInstalls {
  project_id: string;
  /** The project's own installs first, then the global ones that reach its task roles. */
  installs: ProjectInstall[];
  /** How many skills the project wrote. */
  owned_count: number;
  task_roles: AgentRole[];
  skill_caps: SkillCaps;
}

export interface InstallChange {
  install: Install;
  item: CatalogItem;
}

export interface RunLibrary {
  sha256: string | null;
  role?: AgentRole | null;
  project_id?: string | null;
  /** `plugin` and `scope` are absent in runs recorded before the catalog. */
  items: { kind: LibraryKind; name: string; sha256: string; plugin?: string; scope?: InstallScope }[];
  /** The MCP servers the run mounted, by name. */
  mcp_servers?: string[];
  error?: string;
}

export interface RunProjectSkills {
  project_id: string | null;
  sha256: string | null;
  plugin: string;
  skills: { name: string; sha256: string }[];
  error?: string;
}

export async function fetchCatalog(): Promise<Catalog> {
  const res = await fetch(`${API_BASE}/catalog`);
  if (!res.ok) await failed(res, "catalog");
  return res.json();
}

export async function fetchCatalogItem(itemId: string): Promise<CatalogItemDetail> {
  const res = await fetch(`${API_BASE}/catalog/${encodeURIComponent(itemId)}`);
  if (!res.ok) await failed(res, "catalog item");
  return res.json();
}

/** Import (or re-import) this machine's enabled Claude Code plugins and personal skills; new items are installed nowhere. */
export async function importCatalog(): Promise<Catalog> {
  const res = await fetch(`${API_BASE}/catalog/import`, { method: "POST" });
  if (!res.ok) await failed(res, "import");
  return res.json();
}

/** Import (or re-import) this machine's user-level MCP servers (~/.claude.json); new ones are installed nowhere. */
export async function importMcpServers(): Promise<Catalog> {
  const res = await fetch(`${API_BASE}/catalog/mcp/import`, { method: "POST" });
  if (!res.ok) await failed(res, "MCP import");
  return res.json();
}

/** Add an MCP server to the catalog, installed nowhere. 409 when its name is taken. */
export async function createMcpServer(server: { name: string; description?: string } & Partial<McpConfig>): Promise<{
  item: CatalogItem;
}> {
  const res = await fetch(`${API_BASE}/catalog/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(server),
  });
  if (!res.ok) await failed(res, "add MCP server");
  return res.json();
}

/** Change an MCP server; a field left out stays, and the mask as a value keeps the stored one. */
export async function updateMcpServer(
  itemId: string,
  change: { name?: string; description?: string } & Partial<McpConfig>,
): Promise<{ item: CatalogItem }> {
  const res = await fetch(`${API_BASE}/catalog/${encodeURIComponent(itemId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(change),
  });
  if (!res.ok) await failed(res, "update MCP server");
  return res.json();
}

/** Remove an item from the catalog with its installs (not a project's own skill). */
export async function deleteCatalogItem(itemId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/catalog/${encodeURIComponent(itemId)}`, { method: "DELETE" });
  if (!res.ok) await failed(res, "delete");
}

/** Install an item globally or in one project; roles default to every role the scope allows. 409 when it is there. */
export async function installItem(install: {
  item_id: string;
  scope: InstallScope;
  project_id?: string;
  roles?: AgentRole[];
  enabled?: boolean;
}): Promise<InstallChange> {
  const res = await fetch(`${API_BASE}/installs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(install),
  });
  if (!res.ok) await failed(res, "install");
  return res.json();
}

export async function updateInstall(
  installId: string,
  fields: { roles?: AgentRole[]; enabled?: boolean },
): Promise<InstallChange> {
  const res = await fetch(`${API_BASE}/installs/${encodeURIComponent(installId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update install");
  return res.json();
}

/** Uninstall; the item stays in the catalog. */
export async function uninstall(installId: string): Promise<{ deleted: string; item: CatalogItem }> {
  const res = await fetch(`${API_BASE}/installs/${encodeURIComponent(installId)}`, { method: "DELETE" });
  if (!res.ok) await failed(res, "uninstall");
  return res.json();
}

/** Send JSON to a catalog endpoint; a refusal throws the server's reason. */
async function catalogRequest<T>(method: string, path: string, label: string, body?: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}/catalog${path}`, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) await failed(res, label);
  return res.json();
}

/** Upload a SKILL.md or a zip of a skill or plugin directory; `name` renames it. Installed nowhere; 409 when the
 * name is taken. `skipped` lists the binary files left out. */
export function uploadCatalogItem(upload: {
  filename: string;
  content_base64: string;
  name?: string;
}): Promise<{ item: CatalogItem; skipped: LibrarySkipped[] }> {
  return catalogRequest("POST", "/upload", "upload", upload);
}

export interface GitSource {
  /** An https:// repository URL. */
  url: string;
  /** A branch or tag; the default branch when absent. */
  ref?: string;
  /** A directory in the repository to import from. */
  subpath?: string;
}

/** A plugin or skill found in a repository and what importing it would do. */
export interface GitPreviewItem {
  /** Its directory in the repository (or the subpath): what an import names. */
  path: string;
  kind: LibraryKind;
  name: string;
  description: string;
  version: string;
  file_count: number;
  size_bytes: number;
  /** new; update or unchanged (imported from this URL before); conflict (its name is taken or unusable). */
  status: "new" | "update" | "unchanged" | "conflict";
  reason?: string;
  item_id?: string;
  /** Edited here since it was imported (status update or unchanged). */
  modified?: boolean;
}

export interface GitPreview {
  url: string;
  ref: string | null;
  subpath: string;
  commit: string;
  items: GitPreviewItem[];
  skipped: LibrarySkipped[];
}

export interface GitImport {
  url: string;
  commit: string;
  items: CatalogItem[];
  added: string[];
  updated: string[];
  unchanged: string[];
  skipped: LibrarySkipped[];
}

/** Clone a repository and list its plugins and skills; nothing is added. */
export function previewGitRepo(source: GitSource): Promise<GitPreview> {
  return catalogRequest("POST", "/git/preview", "git preview", source);
}

/** Import the chosen items of a previewed repository; 409 when a name is taken or the ref moved since `commit`. */
export function importGitRepo(
  source: GitSource & { commit: string; items: { path: string; name?: string }[] },
): Promise<GitImport> {
  return catalogRequest("POST", "/git/import", "git import", source);
}

/** Write a skill in Settings (origin "ui"); installed nowhere. */
export function createCatalogSkill(skill: NewProjectSkill): Promise<{ item: CatalogItem }> {
  return catalogRequest("POST", "/skills", "new skill", skill);
}

/** Change a Settings, uploaded or git skill; an uploaded or git one is marked modified. */
export function updateCatalogItem(itemId: string, fields: Partial<NewProjectSkill>): Promise<{ item: CatalogItem }> {
  return catalogRequest("PUT", `/${encodeURIComponent(itemId)}`, "edit", fields);
}

/** What applies to a project: its own installs and the inherited global ones. */
export async function fetchProjectInstalls(projectId: string): Promise<ProjectInstalls> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/installs`);
  if (!res.ok) await failed(res, "installs");
  return res.json();
}

// --- Channel configs ---

export interface ChannelConfig {
  channel_id: string;
  channel_type: string;
  display_name: string;
  enabled: boolean;
  /** Mostly environment variable names; a chat channel's sender allowlist is a list of ids. */
  config: Record<string, unknown>;
  /** Why this channel does not start as configured (its sender allowlist); null when nothing stops it. */
  start_problem?: string | null;
  /** Whether a bot token is stored (the token itself never leaves the server). */
  has_bot_token?: boolean;
  created_at: string;
  updated_at: string;
}

/** The Lark (lark-cli) channel as lark-cli sees its saved settings (`GET /api/channels/lark/status`). */
export interface LarkStatus {
  /** The bot lark-cli acts as under the channel's profile; null when it cannot (see `problem`). */
  bot: { name: string; app_id: string; open_id: string; profile: string } | null;
  /** Why the channel would not start (lark-cli, or the allowlist made for another app); null otherwise. */
  problem: string | null;
  /** The app the sender allowlist was made for; open_ids differ per app. */
  allowlist_app_id: string | null;
  /** Names of the allowlist's and the refused senders' open_ids, where lark-cli knows them. */
  names: Record<string, string>;
  /** The latest senders the allowlist refused, newest first, not on the saved allowlist. */
  rejected: { open_id: string; name: string | null; count: number; last_at: string }[];
}

/** A person the people search found: their open_id for the channel's app. */
export interface LarkPerson {
  open_id: string;
  name: string;
  email: string;
  department: string;
}

export async function fetchLarkStatus(): Promise<LarkStatus> {
  const res = await fetch(`${API_BASE}/channels/lark/status`);
  if (!res.ok) await failed(res, "lark status");
  return res.json();
}

export async function searchLarkPeople(query: string): Promise<LarkPerson[]> {
  const res = await fetch(`${API_BASE}/channels/lark/people?q=${encodeURIComponent(query)}`);
  if (!res.ok) await failed(res, "lark people");
  return (await res.json()).people ?? [];
}

export async function fetchChannelConfigs(): Promise<ChannelConfig[]> {
  const res = await fetch(`${API_BASE}/channel-configs`);
  if (!res.ok) throw new Error(`channel-configs ${res.status}`);
  const data = await res.json();
  return data.configs ?? [];
}

export async function fetchChannelConfig(channelId: string): Promise<ChannelConfig> {
  const res = await fetch(`${API_BASE}/channel-configs/${channelId}`);
  if (!res.ok) throw new Error(`channel-config ${res.status}`);
  return res.json();
}

export async function updateChannelConfig(
  channelId: string,
  /** `bot_token`: the mask ("***") keeps the stored token, "" removes it, any other value stores it. */
  fields: Partial<ChannelConfig> & { bot_token?: string },
): Promise<ChannelConfig> {
  const res = await fetch(`${API_BASE}/channel-configs/${channelId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update channel-config");
  return res.json();
}

export function streamSSE(
  eventsUrl: string,
  onEvent: (event: SSEEvent) => void,
  onDone?: () => void,
): () => void {
  const abortController = new AbortController();

  (async () => {
    try {
      const res = await fetch(eventsUrl, { signal: abortController.signal });
      if (!res.ok || !res.body) return;

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        let eventType = "";
        let eventData = "";

        for (const line of lines) {
          if (line.startsWith("event: ")) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            eventData = line.slice(6);
          } else if (line === "" && eventData) {
            try {
              const parsed = JSON.parse(eventData);
              onEvent({ kind: eventType || parsed.kind || "unknown", payload: parsed.payload || parsed });
            } catch {
              // skip non-JSON
            }
            eventType = "";
            eventData = "";
          }
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        console.error("SSE error:", e);
      }
    } finally {
      onDone?.();
    }
  })();

  return () => abortController.abort();
}
