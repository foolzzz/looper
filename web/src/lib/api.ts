import { isSessionEnded, SessionEndedError } from "@/lib/session";
import { callV1, streamV1, V1Error } from "@/lib/v1";

const API_BASE = "/api";

export interface SystemStatus {
  status: "running";
  agents: Record<string, AgentStatus>;
  tasks: { total: number; running: number; completed: number };
  channels: { web: WebHealth };
  /** The startup self-check, as `looper doctor` prints it. */
  checks: SelfCheck[];
  /** True for a password session, which can sign out; false for the local browser's auto-login. */
  can_sign_out: boolean;
  turn_binding?: TurnBindingStatus;
  managers?: { id: string; name: string; running: boolean; busy: boolean; healthy: boolean;
    sync: "pending" | "ok"; sync_error?: string; turn_binding: TurnBindingStatus }[];
}

export interface AgentStatus {
  healthy: boolean;
  state: string;
  up: boolean;
  restart_count: number;
  last_crash_at: string | null;
  next_restart_at: string | null;
  agent_type: string;
}

export interface WebHealth { channel: "web"; healthy: boolean; active_sse_streams: number }

export interface TurnBindingStatus {
  enforced: boolean;
  mismatches: number;
  by: { reason: string; runtime: string; tool: string; count: number; manager?: string }[];
  matches?: { by: string; when: string; count: number }[];
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
  /** What the task is called: its title, else its goal id, else its requirement's first line (server-side). */
  name: string;
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
  /** The Manager the task belongs to ("manager" is the default one) and its name (null if gone). */
  manager_id?: string;
  manager_name?: string | null;
  continues_schedule_session: boolean;
}

/** The agents that work on one task; their runs make up its cost. */
export type TaskRole = "planner" | "worker" | "reviewer";

/** A task's runs, tokens and cost on one model. */
export interface TaskModelCost {
  /** The recorded model, or null when none was recorded. */
  model: string | null;
  runs: number;
  running: number;
  unpriced_runs: number;
  interrupted_runs: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  /** Null when none of the model's runs has a cost. */
  cost_usd: number | null;
}

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
  by_model: TaskModelCost[];
  /** The Manager's turns about the task, by model; not in cost_usd or the spend cap. */
  manager_by_model: TaskModelCost[];
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
  /** Where a step that runs alongside others works: the task directory ("shared", also when null) or, in a git
   * checkout on a branch, a worktree of its own merged into the branch when the step is accepted ("worktree"). */
  step_workspaces: "shared" | "worktree" | null;
  /** The paths Looper's worktrees link back to the directory they come from (its ignored local state, such as
   * .venv or .env): a task's worktree to its workspace, a step's to its task directory; null = none. */
  workspace_links: string[] | null;
  /** Where a task that names no directory of its own runs; null = in place. A new project gets "worktree". */
  task_workspaces: WorkspaceMode | null;
  /** Whether the project's agents get the operator's Claude Code notes for their directory; null = "off". */
  operator_notes: "off" | "on" | null;
  /** How many times the Planner may retry one step, 0 to 20; null = 2. */
  max_step_retries: number | null;
  /** Whether the project's Planners may define the roles their steps run as; null = "off". */
  planner_roles: "off" | "on" | null;
  /** Whether a wait may run a shell command as the server's user; null = "off". */
  wait_commands: "off" | "on" | null;
  /** How long such a role's prompt and its harness may each be, 500 to 20,000 characters; null = 8,000. */
  role_text_max_chars: number | null;
  /** Whether the project's Planners may stop part of a plan at a user gate to ask the user; null = "off". Tasks a
   * schedule starts never open one. */
  user_gates: "off" | "on" | null;
  /** Hours between the reminders of a gate that waits for the user, 0 to 168; 0 = none, null = 24. */
  gate_reminder_hours: number | null;
  /** Whether the agents of the project's tasks share a room they message each other in; null = "on" (unlike the
   * other switches). */
  agent_room: "off" | "on" | null;
  /** Whether the project's agents may use the configured knowledge vault; null = "off". */
  vault: "off" | "on" | null;
  /** The room's limits (`ROOM_LIMITS` in lib/room has each one's range and default); null = the default. */
  room_post_per_minute: number | null;
  room_broadcasts_per_run: number | null;
  room_message_max_chars: number | null;
  room_messages_per_task: number | null;
  room_ping_pong_rounds: number | null;
  room_wait_max_seconds: number | null;
  /** What one consult of a seat whose run ended may spend (USD), how long it may take, how many a task may have. */
  room_consult_budget_usd: number | null;
  room_consult_timeout_seconds: number | null;
  room_consults_per_task: number | null;
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
  const result = await callV1<SystemStatus>("/api/v1/system/status", {});
  if (result.status === "needs_confirmation") throw new V1Error(202, "unavailable", "Status unexpectedly needs confirmation.");
  return result;
}

/** What a task's status means at a glance; the server maps each status to one (an unknown one to "other"). */
export type TaskGroup = "needs_you" | "in_progress" | "ready" | "attention" | "completed" | "cancelled" | "other";

export const TASK_GROUPS: { id: TaskGroup; label: string }[] = [
  // A user gate of its plan waits for the user (status "waiting_user"); other steps may still run.
  { id: "needs_you", label: "Needs you" },
  // "waiting": the plan waits on time or a check, without needing the user (unlike "waiting_user").
  { id: "in_progress", label: "In progress" },
  { id: "ready", label: "Ready" },
  { id: "attention", label: "Needs attention" },
  { id: "completed", label: "Completed" },
  { id: "cancelled", label: "Cancelled" },
  { id: "other", label: "Other" },
];

export function isTaskGroup(value: string | null): value is TaskGroup {
  return TASK_GROUPS.some((g) => g.id === value);
}

/** A task whose Planner runs (also while a gate waits for the user) or is starting: it can be stopped, and must be
 * before it is deleted. */
export function isActiveTask(task: Pick<TaskInfo, "group">): boolean {
  return task.group === "needs_you" || task.group === "in_progress";
}

/** A Planner running now: one per task in progress. */
export interface LivePlanner {
  agent_id: string;
  task_id: string;
  project_id: string | null;
  project_name: string | null;
  requirement: string;
  /** What the task is called (see TaskInfo.name). */
  name: string;
}

/** A Worker or Reviewer run now, running or waiting for room under its task's limit. */
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
  /** The role its task's Planner defined that it runs as; absent for a built-in role's run. */
  task_role?: RunRole;
}

/** How many of one role run in a project, and how many wait for their task's limit. */
export interface RoleLoad {
  running: number;
  waiting: number;
}

/** `system.overview`: what is happening now. */
/** A supervisor component that is not simply running: down and waiting to be restarted ("restarting"), or restarted
 * three times in a row without staying up 5 minutes ("crash_looping", up or down now). It is restarted for as long as
 * the server runs, each wait twice the last, from 1 s up to 5 minutes. */
export interface ComponentHealth {
  name: string;
  state: "restarting" | "crash_looping";
  /** Running now (a crash-looping one between crashes). */
  up: boolean;
  /** Restarts in a row since it last stayed up 5 minutes. */
  restart_count: number;
  last_crash_at: string | null;
  /** While it waits to start again. */
  next_restart_at: string | null;
}

export interface Overview {
  /** The installed Looper version; absent from a server that predates it. */
  version?: string;
  tasks: Record<TaskGroup, number> & { total: number; other_statuses: Record<string, number> };
  agents: {
    /** null when the server has no Manager to ask. */
    manager: { agent_id: string; state: "busy" | "idle" } | null;
    planners: LivePlanner[];
    workers: LiveStepRun[];
    reviewers: LiveStepRun[];
  };
  /** The projects with a task in progress or ready. */
  projects: {
    project_id: string;
    name: string;
    tasks: { running: number; ready: number };
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
  /** The supervisor's components that are not simply running. */
  health: { unhealthy: ComponentHealth[] };
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
  const result = await callV1<Overview>("/api/v1/system/overview", {});
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** A page of the Tasks list, newest first. */
export interface TasksPage {
  tasks: TaskInfo[];
  /** Pass as `before` for the next page; null at the end. */
  next_before: string | null;
  /** How many tasks each status holds under the project, group and schedule filters (not the status one). */
  counts: Record<string, number>;
}

/** A page of tasks: `limit` (default 50, at most 200) of them older than the task `before`, filtered by project,
 * group, schedule and status. */
export async function fetchTasks(
  filters: {
    projectId?: string;
    group?: TaskGroup;
    scheduleId?: string;
    status?: string;
    before?: string | null;
    limit?: number;
  } = {},
): Promise<TasksPage> {
  const params = new URLSearchParams();
  if (filters.projectId) params.set("project_id", filters.projectId);
  if (filters.group) params.set("group", filters.group);
  if (filters.scheduleId) params.set("schedule_id", filters.scheduleId);
  if (filters.status) params.set("status", filters.status);
  if (filters.before) params.set("before", filters.before);
  if (filters.limit) params.set("limit", String(filters.limit));
  const qs = params.toString();
  const res = await fetch(`${API_BASE}/tasks${qs ? `?${qs}` : ""}`);
  if (!res.ok) await failed(res, "tasks");
  return res.json();
}

/** Who a chat message goes to: the global Manager or a running task's Planner. */
export type ChatTarget = { kind: "manager"; id?: string } | { kind: "planner"; task_id: string };

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
  /** Planner: what the task is called (see TaskInfo.name). Manager: the Manager's name. */
  name?: string;
  /** Planner: the task's status; Worker/Reviewer: its latest run's status. */
  status?: string;
  /** Worker/Reviewer: the step it ran (0-based), when known. */
  step_index?: number | null;
  instruction?: string;
  started_at?: string;
  /** Worker/Reviewer: the role its task's Planner defined that it ran as; absent for a built-in role's run. */
  task_role?: RunRole;
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
  options?: { newSessionProfileId?: string },
): Promise<ChatSendResponse> {
  const res = await fetch(`${API_BASE}/chat/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text, session_id: sessionId, message_id: messageId, target,
      ...(options?.newSessionProfileId ? { new_session_profile_id: options.newSessionProfileId } : {}),
    }),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `chat send ${res.status}`);
  }
  return res.json();
}

/** How a Manager reply ended (the `outcome` of `answer.final`). */
export type ChatReplyOutcome = "answered" | "failed" | "stopped" | "confirm";

/** The Manager's running turn for a chat. */
export interface ChatTurn {
  /** The envelope id. */
  message_id: string;
  /** The id the page gave its own message; null when the message did not come from this page. */
  client_message_id: string | null;
  channel: string;
  started_at: string;
  stopping: boolean;
  /** Whether the Manager's runtime can interrupt the reply (Claude Code, Grok); a runtime run per turn cannot. */
  can_interrupt: boolean;
}

/** Stop the Manager's reply to a message. A 409 (no turn running, or another message's turn) resolves with its
 * `state`; other errors throw with the server's reason. */
export async function stopChatReply(messageId: string): Promise<{ state: string; method: string | null }> {
  const res = await fetch(`${API_BASE}/chat/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message_id: messageId }),
  });
  if (res.ok || res.status === 409) {
    const data = await res.json().catch(() => ({}));
    if (res.ok) return { state: data.state, method: data.method ?? null };
    return { state: data.state, method: null };
  }
  return failed(res, "stop reply");
}

/** The chat's system settings (Settings → Chat). */
export interface ChatSettings {
  /** Whether a reply can be stopped (Esc, the Stop button, /stop). Experimental; off by default. */
  stop_enabled: boolean;
}

export async function fetchChatSettings(): Promise<ChatSettings> {
  const res = await fetch(`${API_BASE}/chat-settings`);
  if (!res.ok) await failed(res, "chat settings");
  return res.json();
}

export async function updateChatSettings(body: { stop_enabled: boolean | null }): Promise<ChatSettings> {
  const res = await fetch(`${API_BASE}/chat-settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) await failed(res, "chat settings");
  return res.json();
}

/** The Manager's running turn that answers this web chat, or null when none is running. */
export async function fetchChatTurn(sessionId: string): Promise<ChatTurn | null> {
  const res = await fetch(`${API_BASE}/chat/turn?session_id=${encodeURIComponent(sessionId)}`);
  if (!res.ok) await failed(res, "chat turn");
  const data = await res.json();
  return data.turn ?? null;
}

/** What choosing a profile does to the Manager's session. */
export type ManagerSessionEffect = "current" | "resumes" | "new_session" | "starts";

export interface ManagerProfileChoice {
  number: number;
  profile_id: string;
  name: string;
  runtime: string;
  provider: string;
  model: string;
  effect: ManagerSessionEffect;
  problem: string | null;
}

/** The session view's effective profile: runtime, provider, model and effort as the server words them. */
export interface ManagerSessionProfileSummary {
  profile_id: string;
  name: string;
  runtime: string;
  provider: string;
  model: string;
  effort: string | null;
}

export function sessionProfileLabel(profile: ManagerSessionProfileSummary): string {
  return `${profile.name} — ${profile.runtime} · ${profile.provider} / ${profile.model}${profile.effort ? ` · ${profile.effort}` : ""}`;
}

export interface ManagerSessionView {
  session: { active: boolean; runtime: string | null; profile_id: string | null };
  selected: { profile_id: string; name: string } | null;
  effective: { profile: ManagerSessionProfileSummary | null; source: SettingSource };
  problem: string | null;
  /** What following Settings does to the saved session; null when Settings' profile cannot launch. */
  follow_settings: { name: string; effect: ManagerSessionEffect } | null;
  choices: ManagerProfileChoice[];
}

async function managerSessionResponse<T>(res: Response, what: string): Promise<T> {
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `${what} ${res.status}`);
  }
  return res.json();
}

export async function fetchManagerSession(managerId = "manager", signal?: AbortSignal): Promise<ManagerSessionView> {
  const query = managerId === "manager" ? "" : `?manager=${encodeURIComponent(managerId)}`;
  return managerSessionResponse(await fetch(`${API_BASE}/manager/session${query}`, { signal }), "manager session");
}

/** Choose the Manager session's Agent Profile; null follows Settings. */
export async function setManagerSessionProfile(
  profileId: string | null,
  managerId = "manager",
): Promise<ManagerSessionView & { effect: ManagerSessionEffect; text: string }> {
  const res = await fetch(`${API_BASE}/manager/session/profile`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(managerId === "manager" ? { profile_id: profileId } : { profile_id: profileId, manager: managerId }),
  });
  return managerSessionResponse(res, "manager session profile");
}

/** Why a Lark or Telegram answer did not reach its chat: the channel no longer knew the chat (it restarted), or it
 * could not send the answer. */
export type DeliveryFailure = "no_chat" | "send_failed";

export interface DbChatMessage {
  message_id: string;
  role: string;
  content: string;
  /** The channel the turn came in on: "web", "telegram", "lark" or "lark_bot". */
  channel?: string;
  created_at: string;
  /** Set for a Lark or Telegram answer its channel did not deliver, with the channel's error. */
  delivery_failure?: DeliveryFailure | null;
  delivery_error?: string | null;
}

/** A conversation asked for under one Manager that belongs to another (`managerId`; the server's 409): never shown or
 * continued there. */
export class ForeignConversationError extends Error {
  readonly managerId: string;
  constructor(message: string, managerId: string) {
    super(message);
    this.name = "ForeignConversationError";
    this.managerId = managerId;
  }
}

async function foreignConversation(res: Response): Promise<ForeignConversationError | null> {
  if (res.status !== 409) return null;
  const data = await res.json().catch(() => ({}));
  return new ForeignConversationError(data.error || "this conversation belongs to another Manager", data.manager_id ?? "");
}

/** A session's newest messages (up to 200), oldest first. With `managerId`, a session of another Manager is a
 * `ForeignConversationError`. */
export async function fetchChatMessages(
  sessionId: string,
  signal?: AbortSignal,
  managerId?: string,
): Promise<DbChatMessage[]> {
  const owner = managerId ? `&manager=${encodeURIComponent(managerId)}` : "";
  const res = await fetch(`${API_BASE}/chat/messages?session_id=${encodeURIComponent(sessionId)}${owner}`, { signal });
  const foreign = await foreignConversation(res);
  if (foreign) throw foreign;
  if (!res.ok) return [];
  const data = await res.json();
  return data.messages || [];
}

/** One of the Manager's conversations on any channel, for the Chat page's conversation list (Planner threads are
 * not listed). */
export interface ChatSession {
  session_id: string;
  /** The channel the session belongs to: "web", "telegram", "lark" or "lark_bot". */
  channel: string;
  message_count: number;
  first_message_at: string;
  last_message_at: string;
  /** Who sent the last message ("user" or "assistant") and its first 200 characters. */
  last_role: string;
  last_content: string;
}

export async function fetchChatSessions(managerId = "manager", signal?: AbortSignal): Promise<ChatSession[]> {
  const query = managerId === "manager" ? "" : `?manager=${encodeURIComponent(managerId)}`;
  const res = await fetch(`${API_BASE}/chat/sessions${query}`, { signal });
  if (!res.ok) await failed(res, "list conversations");
  const data = await res.json();
  return data.sessions ?? [];
}

/** One conversation by its id, however long ago it was active (null when it has no messages: a new one): the list holds
 * only the newest. Another Manager's is a `ForeignConversationError`. */
export async function fetchChatSession(
  sessionId: string,
  managerId = "manager",
  signal?: AbortSignal,
): Promise<ChatSession | null> {
  const owner = managerId === "manager" ? "" : `&manager=${encodeURIComponent(managerId)}`;
  const res = await fetch(`${API_BASE}/chat/sessions?session_id=${encodeURIComponent(sessionId)}${owner}`, { signal });
  const foreign = await foreignConversation(res);
  if (foreign) throw foreign;
  if (!res.ok) await failed(res, "load the conversation");
  const data = await res.json();
  return data.sessions?.[0] ?? null;
}

/** How long a dropped chat watch waits before it reconnects. */
const CHAT_WATCH_RETRY_MS = 3000;

/**
 * Follow an open chat for messages that answer none of its own (what the
 * Manager's Claude says on its own): each reaches `onMessage` as it is saved.
 * Reconnects while watched; the returned function stops watching.
 */
export function watchChat(
  sessionId: string,
  onMessage: (message: DbChatMessage) => void,
  managerId?: string,
): () => void {
  const owner = managerId ? `&manager=${encodeURIComponent(managerId)}` : "";
  let stopped = false;
  let stop = () => {};
  let retry: ReturnType<typeof setTimeout> | undefined;
  const connect = () => {
    stop = streamSSE(
      `${API_BASE}/chat/events?session=${encodeURIComponent(sessionId)}${owner}`,
      (event) => {
        if (event.kind === "chat.message") onMessage(event.payload as unknown as DbChatMessage);
      },
      () => {
        if (!stopped && !isSessionEnded()) retry = setTimeout(connect, CHAT_WATCH_RETRY_MS);
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
  const result = await callV1<{ task_id: string; cancelled: boolean }>("/api/v1/ops/task_control/cancel", { task_id: taskId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
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
  const result = await callV1<{ task_id: string; stopped: boolean }>("/api/v1/ops/task_control/stop", { task_id: taskId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
}

/** Throw with the server's reason when it gives one. */
async function failed(res: Response, label: string): Promise<never> {
  const data = await res.json().catch(() => ({}));
  throw new Error(data.error || `${label} ${res.status}`);
}

/** Run a stopped, failed or cancelled task again: from its saved plan, or, with `startOver`, planned anew (its steps,
 * plan and Planner session are deleted). */
export async function startTask(taskId: string, { startOver = false }: { startOver?: boolean } = {}): Promise<void> {
  const action = startOver ? "start_over" : "restart";
  const result = await callV1<{ task_id: string; restarted?: boolean; started_over?: boolean; note: string }>(
    `/api/v1/ops/task_control/${action}`, { task_id: taskId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
}

/** Start a ready task; it runs at once. Fails unless the task is ready. */
export async function startReadyTask(taskId: string): Promise<void> {
  const result = await callV1<{ task_id: string; started: boolean }>("/api/v1/ops/task_control/start", { task_id: taskId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
}

/** Raise the task's own cap, or its project's per-task cap, above its spend and continue the budget-failed task. */
export async function continueTaskAtCap(taskId: string, taskBudgetUsd: number): Promise<void> {
  const result = await callV1<{ task_id: string; project_id: string; task_budget_usd: number;
    spent_usd: number; continued: string }>("/api/v1/tasks/continue", { task_id: taskId, task_budget_usd: taskBudgetUsd });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
}

/** Drop a step worktree a task that is not running keeps: its directory and branch are deleted. */
export async function discardStepWorkspace(taskId: string, workspaceId: number): Promise<StepWorkspace> {
  const res = await fetch(
    `${API_BASE}/tasks/${encodeURIComponent(taskId)}/workspaces/${workspaceId}/discard`, { method: "POST" });
  if (!res.ok) await failed(res, "discard");
  return (await res.json()).workspace;
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
    /** How it runs in its directory; omit for the default (`WorkspaceCheck.default_mode`). */
    workspace_mode?: WorkspaceMode;
    /** Where a worktree task's branch starts: a branch, tag or commit; omit for what the directory has checked out. */
    base?: string;
  } & TaskProjectChoice,
): Promise<{ task_id: string; status: string; created_at: string; project_id: string; workspace: WorkspaceCheck }> {
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

/** How a task runs in its directory: in a git worktree of it, on a branch of its own, or in the directory itself. */
export type WorkspaceMode = "worktree" | "in_place";

/** What a new task in a directory would run in, and what to know before starting it. */
export interface WorkspaceCheck {
  workspace: string;
  /** What it runs in: in_place when a worktree was asked for outside git. */
  mode: WorkspaceMode;
  requested_mode: WorkspaceMode;
  /** The mode a task gets when it does not choose one (the form's default); only from the check. */
  default_mode?: WorkspaceMode;
  /** null outside git. */
  git: { repository: string; branch: string | null; head: string; base_commit?: string } | null;
  warnings: WorkspaceWarning[];
  /** The operator's Claude Code notes for the directory. */
  notes: { found: boolean; files: string[]; omitted: string[]; error?: string };
  /** Whether the project gives its agents those notes; only from the check. */
  notes_on?: boolean;
}

export interface WorkspaceWarning {
  kind: "not_git" | "uncommitted" | "shared" | "large_checkout";
  text: string;
  files?: string[];
  tasks?: { task_id: string; name: string; status: string; mode: WorkspaceMode }[];
}

/** What a task in `dir` (else the project's directory) would run in; no `project_id` for a project about to be made. */
export async function checkWorkspace(
  params: { dir?: string; project_id?: string; mode?: WorkspaceMode; base?: string },
  signal?: AbortSignal,
): Promise<WorkspaceCheck> {
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][]);
  const res = await fetch(`${API_BASE}/workspaces/check?${query}`, { signal });
  if (!res.ok) await failed(res, "check");
  return res.json();
}

/** A task's workspace as its page shows it. */
export interface TaskWorkspaceView {
  workspace_dir: string;
  mode: WorkspaceMode;
  base: string;
  /** The worktree it runs in; null for a task in place, or before its first start. */
  worktree: {
    branch: string;
    base_commit: string;
    path: string;
    repository: string;
    links: string[];
    state: "active" | "removed";
    result_commit: string | null;
    removed_at: string | null;
    /** Run in the workspace: see the task's work, merge it, drop the branch. */
    commands: UndoCommand[];
  } | null;
}

/** Move a task to another project; allowed in any task status. */
export async function setTaskProject(
  taskId: string,
  projectId: string,
): Promise<{ task_id: string; project_id: string; project_name: string }> {
  const data = await callV1<{ task_id: string; project_id: string; project_name: string }>(
    "/api/v1/tasks/move", { task_id: taskId, project_id: projectId });
  if ("status" in data) throw new V1Error(202, "unavailable", data.impact);
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
  changes: { section: string; key: string; action: Exclude<ConfigImportAction, "unchanged">; fields?: string[]; reason?: string; code?: string }[];
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

/** Sent on window when a gate was answered or changed here, so the sidebar's waiting count refreshes at once. */
export const GATES_CHANGED_EVENT = "looper:gates-changed";

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
/** While a run is in progress: skip the firing, queue one, or start runs beside it (reentrant). */
export type ScheduleOverlap = "skip" | "queue" | "parallel";
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
/** "failures": failed runs and the two alerts with their recoveries; "alerts": the alerts and their recoveries
 * only; "always": every run that ran a task, and the alerts; "never": nothing is sent. */
export type ScheduleNotify = "failures" | "alerts" | "always" | "never";

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
  /** The task's title, when it has one. */
  task_title: string | null;
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
  /** How many firings it stands for: a skip that repeated is one run, from scheduled_for to finished_at. */
  firings: number;
}

export interface ScheduleInfo {
  planner_session: {
    active: boolean;
    runtime: string | null;
    profile: string | null;
    last_task_id: string | null;
    last_task_title: string | null;
    /** When the stored session began, in contrast to schedule edits. */
    updated_at: string | null;
  };
  schedule_id: string;
  project_id: string;
  project_name: string;
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
  /** With overlap "parallel": how many runs may be in progress at once. */
  max_parallel_runs: number;
  catch_up: ScheduleCatchUp;
  /** "default": follows the system default (Settings → Scheduler); null: no limit. */
  run_timeout_seconds: number | "default" | null;
  /** What a run that starts now gets (null: no limit). */
  run_timeout_effective_seconds: number | null;
  /** Null: the project's task spend cap applies. */
  run_budget_usd: number | null;
  /** The Agent Profile of some of a run's roles; {} = none. */
  profile_overrides: Partial<Record<TaskRole, string>>;
  /** The Worker and Reviewer limits each run's task gets as its own. */
  task_limits: ScheduleTaskLimits;
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

/** How many Workers and Reviewers run at once in each task a schedule creates; null = the default (Settings → Roles). */
export type ScheduleTaskLimits = Record<TaskLimitRole, number | null>;

export interface ScheduleLimits {
  run_timeout_max_seconds: number;
  name_max_chars: number;
  requirement_max_chars: number;
  /** Fires closer than this make a rule invalid. */
  min_interval_seconds: number;
  /** The same for a schedule with a gate command. */
  min_interval_gated_seconds: number;
  default_timezone: string;
  /** The system default run timeout, for a schedule that follows it. */
  run_timeout_default_seconds: number | null;
  roles: TaskRole[];
  gate_command_max_chars: number;
  gate_timeout_default_seconds: number;
  gate_timeout_max_seconds: number;
  /** The most runs of a parallel schedule in progress at once. */
  max_parallel_runs_limit: number;
  /** The most a task limit (`task_limits`) may be. */
  concurrency_max: number;
  /** Empty: no channel has a sender to send to. */
  notify_targets: ScheduleNotifyTargetOption[];
}

/** A rule checked by the server: its description and next fire times, or why it is invalid. */
export interface SchedulePreview {
  min_interval_seconds: number;
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
  max_parallel_runs?: number;
  catch_up?: ScheduleCatchUp;
  /** "default": the system default (a new schedule's); null: no limit. */
  run_timeout_seconds?: number | "default" | null;
  run_budget_usd?: number | null;
  profile_overrides?: Partial<Record<TaskRole, string>>;
  /** Replaces the schedule's limits, a role left out being null; null clears both. */
  task_limits?: Partial<ScheduleTaskLimits> | null;
  /** "" or null clears it. */
  gate_command?: string | null;
  gate_timeout_seconds?: number | null;
  daily_budget_usd?: number | null;
  notify?: ScheduleNotify;
  notify_target?: ScheduleNotifyTarget | null;
}

/** Any of a schedule's fields, plus turning it on or off. */
export type ScheduleUpdate = Partial<ScheduleFields> & { enabled?: boolean };

/** One of the scheduler's system settings and where its value comes from. */
export interface SchedulerSetting {
  /** Null: no limit (the run timeout's built-in). */
  value: number | null;
  /** "setting" (saved in Settings), "env" (the deprecated variable) or "built-in". */
  source: "setting" | "env" | "built-in";
  built_in: number | null;
  minimum: number;
  maximum?: number;
}

export interface SchedulerSettings {
  min_interval_seconds: SchedulerSetting & { gated_value: number };
  run_timeout_seconds: SchedulerSetting;
}

/** Null goes back to the built-in value. */
export type SchedulerSettingsUpdate = Partial<Record<keyof SchedulerSettings, number | null>>;

export async function fetchSchedulerSettings(): Promise<SchedulerSettings> {
  return scheduleCall<SchedulerSettings>("GET", `${API_BASE}/scheduler-settings`, "scheduler settings");
}

export async function updateSchedulerSettings(body: SchedulerSettingsUpdate): Promise<SchedulerSettings> {
  return scheduleCall<SchedulerSettings>("PUT", `${API_BASE}/scheduler-settings`, "scheduler settings", body);
}

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

/** Every project's schedules, the disabled ones too, by project name and then name (the Schedules page). */
export async function fetchAllSchedules(): Promise<ScheduleInfo[]> {
  const result = await callV1<{ schedules: ScheduleInfo[] }>("/api/v1/schedule_admin/list", {});
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result.schedules;
}

/** A project's schedules, ordered by name, and the limits their fields have. */
export async function fetchSchedules(projectId: string): Promise<{ schedules: ScheduleInfo[]; limits: ScheduleLimits }> {
  const result = await callV1<{ schedules: ScheduleInfo[]; limits: ScheduleLimits }>(
    "/api/v1/schedule_admin/project_list", { project_id: projectId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
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
  const result = await callV1<{ schedule: ScheduleInfo }>("/api/v1/schedule_admin/get", { schedule_id: scheduleId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result.schedule;
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

/** Clear the stored Planner session. A run in progress keeps its own session without writing it back. */
export function resetSchedulePlannerSession(scheduleId: string): Promise<{ cleared: boolean }> {
  return scheduleCall("POST", `${scheduleUrl(scheduleId)}/reset-planner-session`, "reset Planner session");
}

/** Run now, also while disabled: the run is running, queued, skipped_overlap, or failed with its error. */
export async function runSchedule(scheduleId: string): Promise<ScheduleRunInfo> {
  return (await scheduleCall<{ run: ScheduleRunInfo }>("POST", `${scheduleUrl(scheduleId)}/run`, "run schedule")).run;
}

/** A schedule's runs, newest first. */
/** Runs that only say there was nothing to do: the gate found nothing new, or the previous run was still going. */
export type RoutineSkip = "skipped_gate" | "skipped_overlap";

/** A page of a schedule's runs, newest first. */
export interface ScheduleRunsPage {
  runs: ScheduleRunInfo[];
  /** Pass as `before` for the next page; null at the end. */
  next_before: string | null;
  /** How many routine skips the schedule has had in all, shown or not. */
  routine_skips: Record<RoutineSkip, number>;
}

/** A page of a schedule's runs: `limit` (default 50, at most 200), older than the run `before`, and without the
 * routine skips when `skips` is "hide". */
export async function fetchScheduleRuns(
  scheduleId: string,
  options: { limit?: number; before?: string | null; skips?: "show" | "hide" } = {},
): Promise<ScheduleRunsPage> {
  const result = await callV1<ScheduleRunsPage>("/api/v1/schedule_admin/runs", { schedule_id: scheduleId, ...options });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
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
  const result = await callV1<{ notifications: ScheduleNotification[] }>(
    "/api/v1/schedule_admin/notifications", { schedule_id: scheduleId, ...(limit === undefined ? {} : { limit }) });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result.notifications;
}

/** Check a rule: its description and next `count` fire times (5 by default), or why it is invalid. */
export async function previewSchedule(
  fields: { cron: string; timezone?: string; count?: number; gated?: boolean },
): Promise<SchedulePreview> {
  const result = await callV1<SchedulePreview>("/api/v1/schedule_admin/preview", fields);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
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
  enabled?: boolean;
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
  const result = await callV1<ProjectInstalls & { skill: ProjectSkill }>(
    "/api/v1/project_skills/add", { project_id: projectId, skill });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** One skill the project wrote and its SKILL.md body. */
export interface ProjectSkillBody {
  skill: ProjectSkill;
  content: string;
}

export async function fetchProjectSkill(projectId: string, skillId: string): Promise<ProjectSkillBody> {
  const result = await callV1<ProjectSkillBody>(
    "/api/v1/project_skills/get", { project_id: projectId, skill_id: skillId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Change a skill's name, description or body; a bad field is 400, a taken name 409. Switch it with its install. */
export async function updateProjectSkill(
  projectId: string,
  skillId: string,
  fields: Partial<Omit<NewProjectSkill, "enabled">>,
): Promise<ProjectInstalls & { skill: ProjectSkill }> {
  const result = await callV1<ProjectInstalls & { skill: ProjectSkill }>(
    "/api/v1/project_skills/update", { project_id: projectId, skill_id: skillId, fields });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Delete a skill the project wrote: from the catalog and from every project it is installed in. */
export async function deleteProjectSkill(
  projectId: string,
  skillId: string,
): Promise<ProjectInstalls & { deleted: string }> {
  const result = await callV1<ProjectInstalls & { deleted: string }>(
    "/api/v1/project_skills/delete", { project_id: projectId, skill_id: skillId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function updateTaskConfig(
  taskId: string,
  fields: { requirement?: string; work_dir?: string; config?: Record<string, unknown> },
): Promise<void> {
  const result = await callV1<{ task_id: string; requirement: string; work_dir: string; status: string;
    config: Record<string, unknown> }>("/api/v1/tasks/configure", { task_id: taskId, ...fields });
  if ("impact" in result) throw new V1Error(202, "unavailable", result.impact);
}

/** One text a task's requirement has had (version 1 is the text it was created with). */
export interface RequirementVersion {
  version: number;
  text: string;
  /** How it came: "created", "task_page", "manager:<channel>". */
  via: string;
  created_at: string;
  /** When the task's Planner had the edit in a turn; null while it has not. */
  delivered_at: string | null;
}

/** What the user added to a running task: a message sent to its Planner, or one the Manager passed on. */
export interface Supplement {
  text: string;
  /** "chat", "manager:<channel>"; "gate" for the conclusion of a user gate. */
  via: string;
  created_at: string;
  /** When the task's Planner had it in a turn; null while it has not. */
  delivered_at: string | null;
}

/** Something the Manager did to a task through its tools (its audit trail), or asked the user to confirm. */
export interface ManagerAction {
  /** The Manager that acted ("manager" is the default one). */
  manager_id?: string;
  created_at: string;
  /** The chat's channel: "web", "lark", "lark_bot", "telegram". */
  channel: string;
  /** The tool, e.g. "looper_task_control". */
  tool: string;
  arguments: Record<string, unknown>;
  risk: "read" | "write" | "high";
  /** "ok", or "needs_confirmation" while it waited for the user's word. */
  status: "ok" | "needs_confirmation";
  confirmation_id: string | null;
  /** The API client (loopercli) it came through, by name; null or absent for none. */
  client_name?: string | null;
}

/** Edit the requirement of a task that has not ended; its Planner, if it runs, gets the edit in a turn. */
export async function editTaskRequirement(taskId: string, requirement: string): Promise<void> {
  const result = await callV1<{ task_id: string; version: number; requirement: string }>(
    "/api/v1/tasks/requirement_update", { task_id: taskId, requirement });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
}

/** Rename a task, in any status; the stored title is returned (one line, at most 60 characters). */
export async function renameTask(taskId: string, title: string): Promise<{ task_id: string; title: string }> {
  const result = await callV1<{ task_id: string; title: string; changed: string[] }>(
    "/api/v1/ops/task_update", { task_id: taskId, title });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return { task_id: result.task_id, title: result.title };
}

/** The roles with a limit in each task: how many of them run at once there. */
export type TaskLimitRole = "worker" | "reviewer";

/** A Worker's or Reviewer's limit in one task: what applies (`value`; `source` "task", "role" or "builtin"), the
 * task's own (null: none) and the default that applies without it (Settings → Roles, else built-in). */
export interface TaskRoleConcurrency extends Sourced<number> {
  task: number | null;
  default: Sourced<number>;
}

/** One task role's Agent Profile (source "task": the task chose it). */
export interface TaskRoleAgents {
  profile: Sourced<EffectiveProfile>;
  /** The runtime type of the role's profile: the prompt mode below applies to "claude-code" only. */
  runtime_type: string;
  /** What the role's runs use on Claude Code; source "task" (the task chose it), "role" or "builtin". */
  system_prompt_mode: Sourced<SystemPromptMode>;
}

/** A Worker's or Reviewer's: also how many run at once in the task. */
export interface TaskLimitedRoleAgents extends TaskRoleAgents {
  concurrency: TaskRoleConcurrency;
}

/** Each task role's Agent Profile, and the Worker's and Reviewer's limits, for one task, with where each comes from. */
export interface TaskAgents {
  /** The most the task's own limit may be (it is not capped by anything else). */
  concurrency_max: number;
  /** False once the task has completed: its choices are shown, not changed. A failed, stopped or cancelled task
   * still changes them, and its restart runs with them. */
  editable: boolean;
  roles: {
    planner: TaskRoleAgents;
    worker: TaskLimitedRoleAgents;
    reviewer: TaskLimitedRoleAgents;
  };
}

/** The task's own choices to change; null drops one, and the settings layers apply again. */
export interface TaskAgentsUpdate {
  /** An Agent Profile's profile_id (or name) per role. */
  profiles?: Partial<Record<TaskRole, string | null>>;
  /** A value that is not a number goes as typed, so the server's reason shows. */
  concurrency?: Partial<Record<TaskLimitRole, number | string | null>>;
  /** Append or replace per role, on Claude Code; null drops the task's choice. */
  system_prompt_modes?: Partial<Record<TaskRole, SystemPromptMode | null>>;
}

/** Change the task's own Agent Profiles or Worker and Reviewer limits. Resolves to its agents as saved, and, after a
 * profile change, when the new profile applies. */
export async function setTaskAgents(
  taskId: string,
  changes: TaskAgentsUpdate,
): Promise<{ agents: TaskAgents; note?: string }> {
  const data = await callV1<{ task_id: string; agents: TaskAgents; note?: string }>(
    "/api/v1/tasks/agents", { task_id: taskId, ...changes });
  if ("status" in data) throw new V1Error(202, "unavailable", data.impact);
  return { agents: data.agents, note: data.note };
}

export interface TaskDetail extends TaskInfo {
  /** The task template the task was created from, as it said then; null for none. */
  template?: {
    key: string;
    name: string;
    updated_at: string;
    inputs: { key: string; label: string; value: string }[];
    planner_instructions: string;
  } | null;
  /** The task's own title, "" when it has none (then `name` is its goal id or its requirement's first line). */
  title?: string;
  /** The API client the task was created through; null for none. */
  client?: { id: string; name: string } | null;
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
  /** Every version of an edited requirement, oldest first; empty for one never edited. */
  requirement_versions?: RequirementVersion[];
  /** What the user added to the task while it ran, oldest first. */
  supplements?: Supplement[];
  /** What the Manager did to the task through its tools, newest first. */
  manager_actions?: ManagerAction[];
  /** Each task role's Agent Profile, and the Worker's and Reviewer's limits, with where each comes from; null when it
   * cannot be read. */
  agents: TaskAgents | null;
  /** The roles the task's Planner defined for its steps, in the order it first defined them. */
  roles: PlannerRole[];
  /** The built-in Worker's and Reviewer's numbers in the task, beside the roles'. */
  builtin_role_stats: { worker: RoleStats; reviewer: RoleStats };
  /** The user gates of its plan, in plan order; their discussions are at `fetchGateThread`. */
  gates: UserGate[];
  /** Wait steps in plan order. */
  waits?: TaskWait[];
  /** The role each Worker and Reviewer run that ran as one ran as, by agent id; absent in a task without such runs. */
  run_roles?: Record<string, RunRole>;
  /** Its steps' git worktrees, in a project whose parallel steps work in worktrees of their own; null (or absent)
   * when it had none. */
  step_workspaces?: TaskWorkspaces | null;
  /** Its workspace and how it runs there (absent from an older server). */
  task_workspace?: TaskWorkspaceView;
  /** Its agent room in numbers; null when it cannot be read. The messages are at `fetchTaskRoom`. */
  room?: RoomSummary | null;
}

/** Where a step's worktree stands: its step works in it ("active"), its branch is being merged, was merged, could
 * not be merged ("unmerged", with `merge_error`), or it was dropped ("discarded"). */
export type StepWorkspaceState = "active" | "merging" | "merged" | "unmerged" | "discarded";
export type MergeError = "conflict" | "local_changes" | "branch_changed" | "step_branch_changed" | "git_failed";

/** A step's git worktree: a branch of its own, merged into the task directory's branch when the step is accepted. */
export interface StepWorkspace {
  id: number;
  step_id: string;
  /** Its step's label. */
  step_key: string;
  branch: string;
  /** The task directory's branch it merges into, without refs/heads/. */
  target_branch: string;
  /** Where its branch starts; a checkpoint of the task directory's pending changes when `checkpoint`. */
  base_commit: string;
  checkpoint: boolean;
  result_commit: string | null;
  /** The merge commit on the task directory's branch; null until there is one (none for a step that changed nothing). */
  merge_commit: string | null;
  state: StepWorkspaceState;
  merge_error: MergeError | null;
  /** The files that conflicted or were in the way, when it could not be merged. */
  merge_files: string[];
  /** What kept it from merging, as git said it; "" otherwise. */
  detail: string;
  /** The paths linked back to the task directory's in it. */
  links: string[];
  path: string;
  /** When its directory and branch were removed; null while either is there. */
  removed_at: string | null;
  /** The user may discard it while its task is not running. */
  discardable: boolean;
}

/** A command, run in the task directory, that undoes some of what the task did to its branch. */
export interface UndoCommand {
  what: string;
  command: string;
}

export interface TaskWorkspaces {
  /** The task directory's branch and its commit when the task dispatched its first step; null when not recorded. */
  start: { branch: string; commit: string } | null;
  /** Oldest first; a step retried after a failed merge has more than one. */
  workspaces: StepWorkspace[];
  undo: UndoCommand[];
}

// --- A task's agent room: the messages its Planner and its steps' seats send each other (read only here) ---

/** Whether a seat has a run now ("online", or "waiting" for room in its task), had one ("offline"), has not started
 * ("planned") or was dropped with its step before it ran. */
export type RoomPresence = "online" | "waiting" | "offline" | "planned" | "dropped" | "consulting";

/** What members post (info, question, answer, interface, announce); Looper writes notices (to a seat) and
 * escalations (to the Planner). */
export type RoomMessageKind = "info" | "question" | "answer" | "interface" | "announce" | "notice" | "escalation";

/** Why Looper wrote a notice or an escalation. */
export type RoomReason = "ping_pong" | "consult_fallback" | "interface_changed" | "room_full" | "watch";

/** A step's seat: `<role>@<step key>` (worker@s2, backend_rd@s2, reviewer@s2). Gates have none; the Planner is not
 * listed. */
export interface RoomSeat {
  seat: string;
  role: string;
  kind: "work" | "review";
  step: string;
  step_status: string;
  presence: RoomPresence;
  /** Since when its run is online or waiting. */
  since?: string;
}

export interface RoomMessage {
  id: number;
  /** A seat, "planner" or "looper". */
  from: string;
  /** "all", or the seats, roles and "planner" it is for. */
  to: "all" | string[];
  kind: RoomMessageKind;
  body: string;
  /** The message it answers or follows up. */
  reply_to?: number;
  files?: string[];
  commits?: string[];
  reason?: RoomReason;
  /** How it came other than posted by its sender's run: the Planner's reply in a [ROOM] turn, Looper's own, or a
   * consult of an offline seat. */
  via?: "planner_reply" | "looper" | "consult";
  at: string;
  /** A Reviewer's question or answer, which the Planner sees too. */
  cc_planner: boolean;
  /** A question with an answer. */
  answered: boolean;
}

export interface RoomCounts {
  messages: number;
  by_kind: Partial<Record<RoomMessageKind, number>>;
  questions: number;
  unanswered: number;
}

/** A step's messages: those its seats sent, those addressed to them, and the questions they asked or were asked that
 * have no answer. */
export interface RoomStepCounts {
  sent: number;
  received: number;
  unanswered: number;
}

/** A task's room in numbers, as the task detail has it. */
export interface RoomSummary {
  /** Its project has the room on (the default). */
  enabled: boolean;
  counts: RoomCounts;
  /** By step key; only the steps with any. */
  by_step: Record<string, RoomStepCounts>;
}

export interface RoomView extends RoomSummary {
  seats: RoomSeat[];
  /** Oldest first: the newest `limit` (500 at most), or with `after` the newer ones only. */
  messages: RoomMessage[];
  /** Older messages than these are not in it (with `after`: more newer ones than `limit`). */
  more: boolean;
  consults: {
    total: number;
    answered: number;
    by_reason: Record<string, number>;
    cost_usd: number;
    list: {
      seat: string;
      questions: number[];
      state: "queued" | "running" | "answered" | "fallback" | "failed";
      reason: string | null;
      read_only: "runtime" | "snapshot" | null;
      denied_writes: number;
      cost_usd: number | null;
      at: string | null;
    }[];
  };
}

/** The task's room: its seats, its numbers and its messages; with `after` (a message id) only the newer ones. */
export async function fetchTaskRoom(
  taskId: string,
  { signal, ...filters }: { kind?: RoomMessageKind; from?: string; unanswered?: boolean; after?: number;
    limit?: number; signal?: AbortSignal } = {},
): Promise<RoomView> {
  const result = await callV1<RoomView>("/api/v1/tasks/room_view", { task_id: taskId, ...filters }, { signal });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Where a user gate stands: "pending" until its dependencies are done, "open" (waiting for the user's first word),
 * "discussing", then "passed" (closed with conclusions) or "cancelled"; "skipped" when a replan dropped it before
 * it opened. */
export type GateState = "pending" | "open" | "discussing" | "passed" | "cancelled" | "skipped";

export interface TaskWait {
  step: string;
  step_id: string;
  state: "pending" | "waiting" | "ready" | "timed_out" | "cancel_requested" | "done" | "failed" | "skipped";
  outcome: "ready" | "resumed" | "proceeded" | "cancelled" | null;
  topic: string;
  check: { type: "github_pr"; repo: string; number: number; ready_when: "head_changed" | "checks_finished" | "new_comments" }
    | { type: "command"; command: string } | null;
  every_seconds: number | null;
  opened_at: string | null;
  due_at: string | null;
  deadline_at: string | null;
  next_check_at: string | null;
  last_checked_at: string | null;
  checks: number;
  last_result: "ready" | "not_ready" | "error" | null;
  last_output: string;
  last_error: string;
  resolution: string;
  notice: "sent" | "dashboard" | "failed" | null;
  closed_at: string | null;
  depends_on: string[];
}

async function moveWait(taskId: string, step: string, action: "resume" | "cancel"): Promise<TaskWait> {
  const result = await callV1<{ task_id: string; wait: TaskWait }>(
    `/api/v1/waits/${action}`, { task_id: taskId, step });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result.wait;
}

export const resumeWait = (taskId: string, step: string) => moveWait(taskId, step, "resume");
export const cancelWait = (taskId: string, step: string) => moveWait(taskId, step, "cancel");

export interface WaitSetting {
  value: number;
  default: number;
  min: number;
  max: number;
  source: "setting" | "built-in";
}

export interface WaitSettings {
  max_wait_seconds: WaitSetting;
  min_check_seconds: WaitSetting;
}

export type WaitSettingsUpdate = Partial<Record<keyof WaitSettings, number | null>>;

export function getWaitSettings(): Promise<WaitSettings> {
  return scheduleCall("GET", `${API_BASE}/wait-settings`, "wait settings");
}

export function updateWaitSettings(body: WaitSettingsUpdate): Promise<WaitSettings> {
  return scheduleCall("PUT", `${API_BASE}/wait-settings`, "wait settings", body);
}

/** One question a gate asks the user; code numbers them q1, q2, ... */
export interface GateAgendaItem {
  id: string;
  question: string;
  /** Answers the user may pick with one click; [] for a free answer. */
  options: string[];
  status: "open" | "resolved" | "dropped";
  answer: string;
}

/** What a gate that passed settled, what its Planner assumes without the user's word, and what is still open. */
export interface GateSummary {
  decisions: string[];
  assumptions: string[];
  open: string[];
}

/** A step of the plan that waits for the user: a topic and the questions only the user can answer. */
export interface UserGate {
  /** The step's label (TaskStep.step_key); the gate's routes take it. */
  step: string;
  step_id: string;
  state: GateState;
  topic: string;
  agenda: GateAgendaItem[];
  /** What the Planner found that the user should know; "" for nothing. */
  findings: string;
  /** Set once it passed. */
  summary: GateSummary | null;
  /** Why it was cancelled; "" otherwise. */
  resolution: string;
  /** It passed because the user ended the discussion. */
  ended_by_user: boolean;
  /** The steps the turn that closed it added, dropped and stopped. */
  plan_change: { added: string[]; dropped: string[]; stopped: string[] } | null;
  /** The ids of the steps it waits for before it opens. */
  depends_on: string[];
  /** While it waits: for the user's word, or for the Planner to take the user's messages; null otherwise. */
  waiting_on: "user" | "planner" | null;
  /** How many of the user's messages a failed Planner turn had and no later turn has taken. While no newer message
   * waits for the Planner, `waiting_on` is then "user": an automatic retry may still deliver them, and the user's
   * next message sends them again. */
  not_delivered: number;
  /** When the Planner tries those messages again on its own; null when no retry is left (or none failed). */
  retry_at: string | null;
  turns: number;
  reminders: number;
  opened_at: string | null;
  first_reply_at: string | null;
  closed_at: string | null;
  /** The chat the gate's messages go to besides its task page; null when they go to no chat (a dashboard task with
   * no gate target in Settings → Channels). */
  channel_posts: GateChannelPosts | null;
}

/** A channel gate messages can go to. */
export type GateChannel = "lark" | "lark_bot" | "telegram";

/** How the gate's own messages fared in its chat. */
export interface GateChannelPosts {
  channel: GateChannel;
  /** The chat in words, e.g. "Lark group oc_123". */
  target: string;
  sent: number;
  failed: number;
  /** Why the latest message that failed could not be sent; null when none failed. */
  last_error: string | null;
}

/** One message of a gate's discussion: the user's, the Planner's, or Looper's own note. */
export interface GateMessage {
  id: number;
  role: "user" | "planner" | "looper";
  /** user: "message" or "end" (ended the discussion); planner: "reply"; looper: what happened to the gate,
   * "error" when the Planner could not take the user's messages, "late" for a message the user wrote while the gate
   * was closing (it became a supplement to the task). */
  kind: "message" | "end" | "reply" | "opened" | "updated" | "reminder" | "cancelled" | "closed" | "resumed" | "error"
    | "late";
  text: string;
  /** Who wrote it in a group chat; null otherwise. */
  author: string | null;
  /** Where the user wrote it: "dashboard", "lark", "lark_bot", "telegram" or "manager:<channel>". */
  via: string | null;
  /** The agenda item and option the user picked with an option button. */
  agenda_item: string | null;
  option: string | null;
  created_at: string;
}

/** The most characters one message in a gate may have. */
export const GATE_MESSAGE_MAX_CHARS = 8000;

/** A gate's discussion, oldest first. */
export async function fetchGateThread(
  taskId: string,
  step: string,
  signal?: AbortSignal,
): Promise<{ task_id: string; gate: UserGate; messages: GateMessage[] }> {
  const result = await callV1<{ task_id: string; gate: UserGate; messages: GateMessage[] }>(
    "/api/v1/ops/gates/messages", { task_id: taskId, gate: step }, { signal });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** What the server did with a message in a gate: "queued" for the Planner's next turn, or "kept" because the task is
 * not running (the Planner gets it when the task starts again). */
export interface GatePosted {
  task_id: string;
  gate: UserGate;
  message: GateMessage;
  delivered: "queued" | "kept";
}

async function postGate(taskId: string, step: string, action: "messages" | "end", body: object): Promise<GatePosted> {
  const result = await callV1<GatePosted>(
    `/api/v1/ops/gates/${action === "messages" ? "reply" : "end"}`, { task_id: taskId, gate: step, ...body });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** The user's message in a gate that waits for them; an option button sends its option as the text, with the
 * agenda item it answers. 409 once the gate no longer waits or the task completed. */
export function postGateMessage(
  taskId: string,
  step: string,
  message: { text: string } | { text: string; agenda_item: string; option: string },
): Promise<GatePosted> {
  return postGate(taskId, step, "messages", message);
}

/** End a gate's discussion: the Planner's next turn must close it with the conclusions so far, its open questions
 * becoming assumptions. */
export function endGateDiscussion(taskId: string, step: string, text = ""): Promise<GatePosted> {
  return postGate(taskId, step, "end", text.trim() ? { text } : {});
}

/** One role's numbers in a task: its runs, the live steps it ran (a review role: reviewed), and the reviews of its
 * work (a review role's: its own). A rate is null before there is anything to count. */
export interface RoleStats {
  runs: number;
  running: number;
  cost_usd: number;
  /** Finished runs whose reply broke the reply contract, and their share of the finished runs. */
  contract_violations: number;
  contract_violation_rate: number | null;
  steps: number;
  accepted: number;
  failed: number;
  retries: number;
  reviews: { passed: number; not_passed: number };
  review_pass_rate: number | null;
}

/** The role a Worker or Reviewer run ran as: one its task's Planner defined, at the version the run started with. */
export interface RunRole {
  id: string;
  kind: "work" | "review";
  version: number;
}

/** Who wrote a version of a task's role: its Planner, or the user on the task page (the Planner cannot change a
 * role whose current version is the user's). */
export type RoleVia = "planner" | "user";

/** A catalog item a role or a template names; its runs load it besides the built-in role's installs while it is
 * installed for roles (an install's "task_role"). kind and name are null for one the catalog no longer has. */
export interface RoleItem {
  id: string;
  kind: LibraryKind | null;
  name: string | null;
  description?: string;
}

/** A role a task's Planner defined: "work" runs a step in the built-in Worker's place; "review" reviews a step
 * after the built-in Reviewer passed it, and both must pass. */
export interface PlannerRole {
  /** Unique in the task. */
  id: string;
  kind: "work" | "review";
  /** Its current version; each new definition is one more. */
  version: number;
  /** null: the profile the step's built-in role of its kind would get. One the project no longer allows
   * (allowed false) counts as none. */
  profile: { profile_id: string; name: string; allowed: boolean } | null;
  role_prompt: string;
  /** null: the working-style part of the harness that applies. */
  harness: string | null;
  reason: string;
  created_at: string;
  /** Who wrote the current version. */
  via: RoleVia;
  /** The project role template the current version was copied from, by key. */
  template: string | null;
  /** The catalog items the current version names. */
  items: RoleItem[];
  /** Every version, oldest first. */
  versions: { version: number; created_at: string; reason: string; via: RoleVia; template: string | null; sha256: string }[];
  /** The labels of the live steps that name it. */
  steps: string[];
  stats: RoleStats;
}

/** A task role's whole definition as the user writes it on the task page; its kind stays. null harness: the working
 * style that applies; null profile: as the built-in role of its kind. */
export interface TaskRoleEdit {
  role_prompt: string;
  harness: string | null;
  /** One the project allows its Planners (its list, or any while the list is empty) that can run the kind. */
  profile_id: string | null;
  /** Item ids, each installed for roles in the project; left out keeps the current ones. */
  items?: string[];
}

/** Write `fields` as a new version of the task's role `key`, by the user; its Planner hears of it. Fails for a
 * completed task, or a definition that changes nothing. */
export async function editTaskRole(
  taskId: string,
  key: string,
  fields: TaskRoleEdit,
): Promise<{ id: string; version: number; changed: string[] }> {
  const result = await callV1<{ task_id: string; id: string; kind: "work" | "review"; version: number;
    via: "user"; changed: string[] }>("/api/v1/tasks/role_update", { task_id: taskId, key, ...fields });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** What a task journal entry records; code writes each when its event happens. */
export type JournalKind =
  | "step_accepted"
  | "step_failed"
  | "step_retry"
  | "decision"
  | "supplement"
  | "session_switch"
  | "handoff"
  | "progress"
  | "profile_choice"
  | "concurrency_change"
  | "role_defined"
  | "gate_opened"
  | "gate_updated"
  | "gate_cancelled"
  | "gate_closed";

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

/** A review role a step names. */
export interface StepReviewRole {
  role: string;
  required: boolean;
  version: number | null;
}

/** The review role a step in review waits for; null while it waits for the built-in Reviewer, or is not in review. */
export function reviewingRole(step: TaskStep): StepReviewRole | null {
  if (step.review_stage !== "role" || step.review_position == null) return null;
  return step.review_roles[step.review_position] ?? null;
}

export interface TaskStep {
  id: string;
  task_id: string;
  step_index: number;
  /** The Planner's label for the step, unique within the task. */
  step_key: string;
  /** "gate": a user gate (`TaskDetail.gates`); "wait": time or a check (`TaskDetail.waits`). Instruction is the topic. */
  kind: "work" | "gate" | "wait";
  instruction: string;
  /** How to tell the step is done, as the Planner wrote it; checked by its Worker and its Reviewer. */
  acceptance_criteria: string[];
  target_files: string[];
  /** The ids of the steps it needs before it starts. */
  depends_on: string[];
  /** pending, dispatched, deciding, completed (in review), verified, failed or skipped (dropped by a replan); a gate
   * is waiting_user while it waits, verified once it passed or was cancelled. A wait is waiting until the Planner
   * settles it, then verified, failed or skipped. */
  status: string;
  /** Whether a Reviewer must pass it. */
  review: boolean;
  /** The role its Worker runs as; null: the built-in Worker. */
  role_key: string | null;
  /** The version of that role its latest attempt ran with; null before it ran as one. */
  role_version: number | null;
  /** The roles that review it after the built-in Reviewer passed it, in order; each blocks the step (required) or
   * only advises the Planner, and version is the one its latest review ran with (null before one ran). */
  review_roles: StepReviewRole[];
  /** Which review a step in review waits for: the built-in Reviewer's or a review role's, the one at
   * review_position in review_roles. */
  review_stage: "builtin" | "role" | null;
  review_position: number | null;
  retries: number;
  worker_id: string | null;
  reviewer_id: string | null;
  result: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
  /** The last time it went to review or ended; a step running again keeps it. */
  completed_at: string | null;
  /** When its run was first dispatched, kept through its retries and reviews; null while it is not started (again,
   * after a restart), and for a step from before starts were kept. */
  started_at: string | null;
}

export interface ActivityEvent {
  seq: number;
  task_id: string;
  worker: string | null;
  kind: string;
  summary: string | null;
  detail?: string | null;
  metadata: Record<string, unknown> | null;
  input_tokens: number | null;
  output_tokens: number | null;
  created_at: string;
}

export type ConversationEvent = Pick<ActivityEvent, "seq" | "worker" | "kind" | "summary" | "created_at">;

export class TaskDetailError extends Error {
  readonly status: number;
  constructor(status: number) {
    super(`task detail ${status}`);
    this.status = status;
  }
}

export async function fetchTaskDetail(taskId: string, signal?: AbortSignal): Promise<TaskDetail> {
  const res = await fetch(`${API_BASE}/tasks/${taskId}`, { signal });
  if (!res.ok) throw new TaskDetailError(res.status);
  return res.json();
}

export async function fetchTaskActivity(
  taskId: string,
  since = 0,
  limit = 100,
): Promise<{ activity: ActivityEvent[] }> {
  const data = await callV1<{ activity: ActivityEvent[] }>("/api/v1/tasks/activity", { task_id: taskId, since, limit });
  if ("status" in data) throw new V1Error(202, "unavailable", data.impact);
  return data;
}

/** A window of a task's events or conversation, oldest first: the latest ones, or those just before `before`. */
export interface EventWindow<T = ActivityEvent> {
  events: T[];
  hasMore: boolean;
}

/** The latest events of a task (1000), or those just before the seq `before`. */
export async function fetchTaskActivityWindow(
  taskId: string,
  before?: number,
  signal?: AbortSignal,
): Promise<EventWindow> {
  const data = await callV1<{ activity: ActivityEvent[]; has_more?: boolean }>(
    "/api/v1/tasks/activity", { task_id: taskId, before }, { signal });
  if ("status" in data) throw new V1Error(202, "unavailable", data.impact);
  return { events: data.activity, hasMore: !!data.has_more };
}

/** The latest messages of a task's conversation (1000), or those just before the seq `before`. */
export async function fetchTaskConversation(
  taskId: string,
  before?: number,
  signal?: AbortSignal,
): Promise<EventWindow<ConversationEvent>> {
  const data = await callV1<{ rows: ConversationEvent[]; has_more: boolean }>(
    "/api/v1/tasks/conversation", { task_id: taskId, before }, { signal });
  if ("status" in data) throw new V1Error(202, "unavailable", data.impact);
  return { events: data.rows, hasMore: data.has_more };
}

/** One run of a task as `GET /api/tasks/<id>/runs` answers: no prompts, metadata body or events. */
export interface TaskRunRow {
  id: number;
  role: TaskRole;
  agent_id: string;
  step_id: string | null;
  status: LlmRecord["status"];
  created_at: string;
  runtime: { type: string | null; name: string | null };
  model: string | null;
  effort: string | null;
  profile: { name: string | null; source: string | null };
  /** The role its task's Planner defined that it ran as; null for a built-in role's run. */
  task_role?: RunRole | null;
}

/** A task's Planner, Worker and Reviewer runs, oldest first, in one compact request. */
export async function fetchTaskRuns(taskId: string, signal?: AbortSignal): Promise<TaskRunRow[]> {
  const res = await fetch(`${API_BASE}/tasks/${taskId}/runs`, { signal });
  if (!res.ok) throw new Error(`runs ${res.status}`);
  const data = await res.json();
  return data.runs ?? [];
}

export function streamTaskActivity(
  taskId: string,
  since: number,
  onEvent: (event: ActivityEvent) => void,
  onDone?: () => void,
): () => void {
  const abortController = new AbortController();
  const signal = abortController.signal;
  let cursor = since;
  let done = false;
  (async () => {
    try {
      while (!signal.aborted && !isSessionEnded() && !done) {
        try {
          const pending = await streamV1<ActivityEvent | Record<string, never>>(
            "/api/v1/tasks/activity_stream", { task_id: taskId, since: cursor }, ({ event, data }) => {
              if (event === "done") done = true;
              else if (event === "activity" && "seq" in data && data.seq > cursor) {
                cursor = data.seq;
                onEvent(data as ActivityEvent);
              }
            }, { signal });
          if (pending) throw new V1Error(202, "unavailable", pending.impact);
        } catch (error) {
          if (signal.aborted || error instanceof SessionEndedError || (error instanceof V1Error && error.status < 500)) throw error;
          console.error("Activity SSE error:", error);
        }
        if (!signal.aborted && !isSessionEnded() && !done) await new Promise<void>((resolve) => {
          const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
          const timer = setTimeout(finish, 1000);
          signal.addEventListener("abort", finish, { once: true });
        });
      }
    } catch (error) {
      if (!signal.aborted && !(error instanceof SessionEndedError)) console.error("Activity SSE error:", error);
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
/** Where a run's setting came from: "task_role" is the profile of the role its task's Planner defined that it ran as;
 * runs recorded before Agent Profiles were named say "agent" for "profile". */
export type RunSettingSource = SettingSource | "task_role" | "agent";
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
  /** A run as a role its task's Planner defined: that role, at the version the run started with. */
  task_role?: RunRole;
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
  /** `interrupted`: Looper stopped the run before it ended (a kill, its task or the server stopping), or the server
   * running it died. */
  status: "running" | "completed" | "failed" | "interrupted";
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
    /** The task step a Worker or Reviewer ran. */
    step_id?: string;
    /** The system prompt Looper supplied to the runtime. */
    system_prompt?: string;
    system_prompt_sha256?: string;
    /** Missing on older runs, which appended to the runtime's default. */
    system_prompt_mode?: SystemPromptMode;
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
  /** A Worker or Reviewer run as a role its task's Planner defined; absent for a built-in role's run. */
  task_role?: RunRole;
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

export async function fetchAgentUsage(agentId: string, turns = 100, signal?: AbortSignal): Promise<AgentUsage> {
  const res = await fetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/usage?turns=${turns}`, { signal });
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
  /** Exact accepted model ids, or null when the provider's catalog is unrestricted by the runtime. */
  supported_models: string[] | null;
  /** Accepted effort levels, or null when the runtime uses the registry-wide choices. */
  effort_levels?: string[] | null;
  can_replace_system_prompt: boolean;
  replace_system_prompt_refusal: string | null;
  /** Whether its Manager or Planner session can interrupt a reply (a user's stop is offered). */
  can_interrupt: boolean;
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

/** Prices are USD per million tokens. */
export type PriceField = "input_usd_per_mtok" | "output_usd_per_mtok" | "cache_read_usd_per_mtok" | "cache_write_usd_per_mtok";

/** An environment variable a provider adds to the runs that use it. */
export interface ProviderEnvEntry {
  key: string;
  value: string;
}

/** A runtime's own login as the server last read it: "chatgpt" (Codex's ChatGPT login, what its login provider
 * runs with), "logged_in" (Grok's login file is there), "other_method", "not_logged_in", "unknown", or
 * "unavailable" while a forked login a run found is not cleared. */
export interface ProviderLogin {
  state: "chatgpt" | "logged_in" | "other_method" | "not_logged_in" | "unknown" | "unavailable";
  detail: string;
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

/** Where a profile is selected: the settings layers (a project's with its name), how many tasks chose it, and the
 * projects whose Planners may choose it. */
export interface ProfileUsedBy {
  layers: { scope: SettingLayer; role: AgentRole | null; project_id: string | null; project_name: string | null }[];
  task_count: number;
  schedule_count: number;
  /** The names of the projects whose list of profiles the Planner may choose holds it. */
  allowed_in: string[];
  /** The Manager's current session runs on it. */
  manager_session?: boolean;
}

export type SystemPromptMode = "append" | "replace";

export interface RegistryLimits {
  name_max_chars: number;
  models_max: number;
  model_fields: string[];
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

export type ApiProtocol = "anthropic-messages" | "openai-chat" | "openai-responses";
export type VendorKind = "glm-bigmodel" | "glm-zai" | "deepseek" | "kimi-moonshot-cn" | "kimi-moonshot"
  | "cpa" | "opencode" | "sub2api" | "claude-subscription" | "codex-login" | "grok-login" | "custom";

export interface ProviderEndpoint {
  provider_id?: string;
  protocol: ApiProtocol;
  base_url: string;
  /** Only migrated endpoints may retain the runtime's default URL. */
  use_runtime_default: boolean;
}

/** An account owns endpoints and credentials; callable models and prices live separately. */
export interface Provider {
  provider_id: string;
  name: string;
  vendor_type: VendorKind;
  auth: ProviderAuth;
  oauth_runtime_type: string | null;
  endpoints: ProviderEndpoint[];
  suggested_models: string[];
  login: ProviderLogin | null;
  env: ProviderEnvEntry[];
  max_concurrency: number | null;
  api_key: string | null;
  builtin: boolean;
  endpoint_count: number;
  model_count: number;
  profile_count: number;
  created_at: string;
  updated_at: string;
}

export interface Model {
  model_id: string;
  provider_id: string;
  provider_name: string;
  name: string;
  protocol: ApiProtocol;
  context_window: number | null;
  input_usd_per_mtok: number | null;
  output_usd_per_mtok: number | null;
  cache_read_usd_per_mtok: number | null;
  cache_write_usd_per_mtok: number | null;
  efforts: string[] | null;
  background_model: string | null;
  catalog_order: number;
  builtin: boolean;
  profile_count: number;
  endpoint: ProviderEndpoint;
  prices: Record<PriceField, { override: number | null; effective: number | null; source: "override" | "builtin" | "unknown" }>;
  warnings: string[];
  created_at: string;
  updated_at: string;
}

/** Creation defaults only; selecting a vendor never registers its suggested models. */
export interface VendorType {
  vendor_type: VendorKind;
  auth: ProviderAuth;
  oauth_runtime_type: string | null;
  endpoints: ProviderEndpoint[];
  suggested_models: (Partial<Pick<Model, "context_window" | "background_model" | "efforts" | PriceField>>
    & Pick<Model, "name" | "protocol">)[];
  env: ProviderEnvEntry[];
  requires_url_confirmation: boolean;
}

export interface AgentProfile {
  profile_id: string;
  name: string;
  model_id: string;
  model: Pick<Model, "model_id" | "name" | "protocol" | "provider_id">;
  effort: string | null;
  agent_teams: boolean;
  custom_prompt: string;
  runtime_id: string;
  runtime_name: string;
  runtime_type: string;
  provider: Pick<Provider, "provider_id" | "name" | "vendor_type" | "auth">;
  builtin: boolean;
  roles: AgentRole[];
  used_by: ProfileUsedBy;
  cost: ProfileCostBasis;
  warnings: string[];
}

export interface Registry {
  runtime_types: RuntimeType[];
  runtimes: Runtime[];
  providers: Provider[];
  models: Model[];
  profiles: AgentProfile[];
  vendor_types: VendorType[];
  prices: { as_of: string; sources: string[]; models: Record<string, Record<PriceField, number | null>> };
  limits: RegistryLimits;
}

export interface ProviderFields {
  name?: string;
  vendor_type?: VendorKind;
  endpoints?: ProviderEndpoint[];
  env?: ProviderEnvEntry[];
  max_concurrency?: number | string | null;
  /** Omitted or masked keeps the key; null removes it. */
  api_key?: string | null;
}

export interface ProfileFields {
  name?: string;
  runtime_id?: string;
  model_id?: string;
  effort?: string | null;
  agent_teams?: boolean;
  custom_prompt?: string;
}

/** Mutable model settings. Empty numeric inputs clear overrides; invalid text is validated by the server. */
export type ModelFields = Partial<Record<PriceField | "context_window", number | string | null>> & {
  efforts?: string[] | null;
  background_model?: string | null;
};

/** A model's account, exact name and protocol are immutable after creation. */
export type CreateModelFields = ModelFields & Pick<Model, "provider_id" | "name" | "protocol">;

export interface PromptBlock {
  name: string;
  owner: "looper" | "runtime";
  text: string | null;
  note: string;
}

export interface PromptPreview {
  profile: { id: string; name: string };
  role: AgentRole;
  runtime: string;
  /** What the launch uses: the role's mode on Claude Code, append elsewhere. */
  system_prompt_mode: SystemPromptMode;
  role_system_prompt_mode: SystemPromptMode;
  custom_prompt_set: boolean;
  looper_prompt_sha256: string;
  prompt_layers: Record<string, unknown>;
  blocks: PromptBlock[];
  notes: string[];
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

export function createProvider(fields: ProviderFields): Promise<{ provider: Provider; warnings: string[] }> {
  return registryCall("POST", "/providers", fields, "add provider");
}

export function updateProvider(
  providerId: string,
  fields: ProviderFields,
): Promise<{ provider: Provider; warnings: string[] }> {
  return registryCall("PUT", `/providers/${encodeURIComponent(providerId)}`, fields, "update provider");
}

export function deleteProvider(providerId: string): Promise<{ deleted: string }> {
  return registryCall("DELETE", `/providers/${encodeURIComponent(providerId)}`, undefined, "delete provider");
}

export function createModel(fields: CreateModelFields): Promise<{ model: Model; warnings: string[] }> {
  return registryCall("POST", "/models", fields, "add model");
}

export function updateModel(modelId: string, fields: ModelFields): Promise<{ model: Model; warnings: string[] }> {
  return registryCall("PUT", `/models/${encodeURIComponent(modelId)}`, fields, "update model");
}

export function deleteModel(modelId: string): Promise<{ deleted: string }> {
  return registryCall("DELETE", `/models/${encodeURIComponent(modelId)}`, undefined, "delete model");
}

/** After checking a runtime's own login on the machine (Codex's, Grok's): let runs use it again after a run found it
 * forked. */
export function clearLoginProblem(providerId: string): Promise<{ provider: Provider }> {
  return registryCall("DELETE", `/providers/${encodeURIComponent(providerId)}/login-problem`, undefined,
    "use the login again");
}

/** Register an Agent Profile; refused (400) when its runtime cannot use the selected model and its derived account. */
export function createAgentProfile(fields: ProfileFields): Promise<{ profile: AgentProfile; warnings: string[] }> {
  return registryCall("POST", "/agent-profiles", fields, "add Agent Profile");
}

export function updateAgentProfile(
  profileId: string,
  fields: ProfileFields,
): Promise<{ profile: AgentProfile; warnings: string[] }> {
  return registryCall("PUT", `/agent-profiles/${encodeURIComponent(profileId)}`, fields, "update Agent Profile");
}

export async function fetchPromptPreview(profileId: string, role: AgentRole, projectId?: string): Promise<PromptPreview> {
  const query = new URLSearchParams({ role });
  if (projectId !== undefined) query.set("project_id", projectId);
  const res = await fetch(`${API_BASE}/agent-profiles/${encodeURIComponent(profileId)}/prompt-preview?${query}`);
  if (!res.ok) await failed(res, "preview system prompt");
  return res.json();
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
/** Where a value came from: a layer, the selected profile itself (its effort), a task's choice of profile, or its
 * Planner's choice of its Workers' or Reviewers' profile, for the rest of the task or for one step. */
export type SettingSource = SettingLayer | "session" | "profile" | "task" | "planner_task" | "planner_step";
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
  /** How many of the role run at once in each task that sets none of its own; only the Worker's and the Reviewer's
   * layer in Settings → Roles sets it (null at every other layer). */
  max_concurrency: number | null;
  /** What every run of a role a task's Planner defines ends with; only the system layer sets it (null: the
   * built-in boundaries). */
  boundaries: string | null;
  /** Whether the role replaces Claude Code's default system prompt or appends to it; only a role layer that sets it
   * has one (absent: the role's built-in value, which `effective` shows). */
  system_prompt_mode?: SystemPromptMode | null;
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
  /** The Workers' and Reviewers' default limit per task (source "role" or "builtin"), in Settings → Roles only;
   * absent for the other roles, the system layer and in a project. */
  max_concurrency?: Sourced<number>;
  /** The role's mode (its role layer's, else the built-in one); it applies to every runtime. */
  system_prompt_mode: SystemPromptMode;
}

export interface SettingLimits {
  harness_max_chars: number;
  env_max_entries: number;
  builtin_harness_prompt: string;
  /** The boundaries a run of a role a task's Planner defines ends with when the shared defaults set none. */
  builtin_boundaries: string;
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
  /** `boundaries`: what the runs of roles a task's Planner defines end with, and where it comes from. */
  system: LayerState & { boundaries: { value: string; source: "builtin" | "system" } };
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

/** An Agent Profile a project's Planners may choose for their task's Workers or Reviewers. */
export interface AllowedProfile {
  profile_id: string;
  name: string;
  /** The runtime's type. */
  runtime: string;
  model: string;
  effort: string | null;
  /** Which of the Worker and the Reviewer it can run. */
  roles: ("worker" | "reviewer")[];
  /** null where the price is unknown. */
  usd_per_million_tokens: { input: number | null; output: number | null };
}

/** The profiles a project's Planners may choose: its list (`profiles`, empty by default, which lets them choose any
 * profile) and what they may choose now (`choosable`: the list, or every profile that can run a Worker or Reviewer). */
export interface AllowedProfiles {
  project_id: string;
  restricted: boolean;
  profiles: AllowedProfile[];
  choosable: AllowedProfile[];
}

export async function fetchAllowedProfiles(projectId: string): Promise<AllowedProfiles> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/allowed-profiles`);
  if (!res.ok) await failed(res, "allowed profiles");
  return res.json();
}

/** Make `profileIds` the whole list ([] lifts the restriction); a profile it drops takes the Planners' choices of it along.
 * Resolves to the list as saved. */
export function setAllowedProfiles(projectId: string, profileIds: string[]): Promise<AllowedProfiles> {
  return putJson(
    `/projects/${encodeURIComponent(projectId)}/allowed-profiles`,
    { profile_ids: profileIds },
    "update allowed profiles",
  );
}

/** A role a project keeps for its tasks' Planners to copy: checked as a task role is, whether or not the project's
 * Planners define roles. */
export interface RoleTemplate {
  /** Unique in the project; the id a copied role gets unless the Planner gives another. */
  key: string;
  kind: "work" | "review";
  /** One line for the Planner: when the template fits a step. */
  description: string;
  role_prompt: string;
  harness: string | null;
  /** One the project no longer allows (allowed false) is left out of a copy. */
  profile: { profile_id: string; name: string; allowed: boolean } | null;
  /** One no longer installed for roles here is left out of a copy. */
  items: RoleItem[];
  updated_at: string;
}

export interface RoleTemplates {
  project_id: string;
  /** How long a template's role prompt and harness may each be. */
  role_text_max_chars: number;
  templates: RoleTemplate[];
  /** The catalog items installed for roles here, which a role or a template may name. */
  role_items: RoleItem[];
}

export type RoleTemplateFields = Pick<RoleTemplate, "kind" | "description" | "role_prompt" | "harness"> & {
  profile_id: string | null;
  items: string[];
};

/** The roles a project's Planners defined, summed by id across the project's tasks with roles; one id may be
 * another definition in each task. builtin: the built-in Worker's and Reviewer's numbers in the same tasks. */
export interface ProjectRoleStats {
  project_id: string;
  tasks: number;
  roles: { id: string; kinds: ("work" | "review")[]; tasks: number; templates: string[]; stats: RoleStats }[];
  builtin: { worker: RoleStats; reviewer: RoleStats };
}

export async function fetchProjectRoleStats(projectId: string): Promise<ProjectRoleStats> {
  const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectId)}/role-stats`);
  if (!res.ok) await failed(res, "role numbers");
  return res.json();
}

const roleTemplatesPath = (projectId: string) => `/projects/${encodeURIComponent(projectId)}/role-templates`;

export async function fetchRoleTemplates(projectId: string): Promise<RoleTemplates> {
  const res = await fetch(`${API_BASE}${roleTemplatesPath(projectId)}`);
  if (!res.ok) await failed(res, "role templates");
  return res.json();
}

/** Create template `key` or replace it whole; resolves to the project's templates. */
export function putRoleTemplate(projectId: string, key: string, fields: RoleTemplateFields): Promise<RoleTemplates> {
  return putJson(`${roleTemplatesPath(projectId)}/${encodeURIComponent(key)}`, fields, "save role template");
}

/** Delete template `key`; the task roles copied from it keep their versions. */
export async function deleteRoleTemplate(projectId: string, key: string): Promise<RoleTemplates> {
  const res = await fetch(`${API_BASE}${roleTemplatesPath(projectId)}/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
  if (!res.ok) await failed(res, "delete role template");
  return res.json();
}

// --- The capability catalog: items acquired into Looper, and where they are installed ---

export type LibraryKind = "plugin" | "skill" | "mcp";
/** How an item was acquired: imported from this machine, uploaded, taken from git, written in Settings ("ui": a skill
 * or an MCP server) or in a project. */
export type ItemOrigin = "machine" | "upload" | "git" | "ui" | "project";
export type AgentRole = "manager" | "planner" | "worker" | "reviewer";
/** An install's roles: the built-in ones, and "task_role", the roles a task's Planner defines: such an install loads
 * only for a run of a role that names its item. */
export type InstallRole = AgentRole | "task_role";
export const TASK_ROLE: InstallRole = "task_role";
/** An install's role in words: a built-in role's name, or what TASK_ROLE means. */
export const installRoleLabel = (role: InstallRole) => (role === TASK_ROLE ? "roles naming it" : role);
/** The roles a new install names unless the user picks: the built-in ones, never TASK_ROLE. */
export const defaultInstallRoles = (roles: InstallRole[]) => roles.filter((role) => role !== TASK_ROLE);
export type InstallScope = "global" | "project";

/** An item installed on a scope for some roles; a project install never names the manager. */
export interface Install {
  install_id: string;
  item_id: string;
  scope: InstallScope;
  project_id: string | null;
  project_name: string | null;
  roles: InstallRole[];
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
  roles: InstallRole[];
  task_roles: InstallRole[];
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
  task_roles: InstallRole[];
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
  const result = await callV1<Catalog>("/api/v1/catalog_items/list", {});
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function fetchCatalogItem(itemId: string): Promise<CatalogItemDetail> {
  const result = await callV1<CatalogItemDetail>("/api/v1/catalog_items/get", { item_id: itemId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Import (or re-import) this machine's enabled Claude Code plugins and personal skills; new items are installed nowhere. */
export async function importCatalog(): Promise<Catalog> {
  const result = await callV1<Catalog>("/api/v1/catalog_sources/import_machine", {});
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Import (or re-import) this machine's user-level MCP servers (~/.claude.json); new ones are installed nowhere. */
export async function importMcpServers(): Promise<Catalog> {
  const result = await callV1<Catalog>("/api/v1/catalog_sources/import_mcp", {});
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Add an MCP server to the catalog, installed nowhere. 409 when its name is taken. */
export async function createMcpServer(server: { name: string; description?: string } & Partial<McpConfig>): Promise<{
  item: CatalogItem;
}> {
  const result = await callV1<{ item: CatalogItem }>("/api/v1/catalog_sources/create_mcp", server);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Change an MCP server; a field left out stays, and the mask as a value keeps the stored one. */
export async function updateMcpServer(
  itemId: string,
  change: { name?: string; description?: string } & Partial<McpConfig>,
): Promise<{ item: CatalogItem }> {
  const result = await callV1<{ item: CatalogItem }>("/api/v1/catalog_items/update", { item_id: itemId, fields: change });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Remove an item from the catalog with its installs (not a project's own skill). */
export async function deleteCatalogItem(itemId: string): Promise<void> {
  const result = await callV1<{ deleted: string }>("/api/v1/catalog_items/delete", { item_id: itemId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
}

/** Install an item globally or in one project; roles default to every role the scope allows. 409 when it is there. */
export async function installItem(install: {
  item_id: string;
  scope: InstallScope;
  project_id?: string;
  roles?: InstallRole[];
  enabled?: boolean;
}): Promise<InstallChange> {
  const result = await callV1<InstallChange>("/api/v1/installs/create", install);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function updateInstall(
  installId: string,
  fields: { roles?: InstallRole[]; enabled?: boolean },
): Promise<InstallChange> {
  const result = await callV1<InstallChange>("/api/v1/installs/update", { install_id: installId, ...fields });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Uninstall; the item stays in the catalog. */
export async function uninstall(installId: string): Promise<{ deleted: string; item: CatalogItem }> {
  const result = await callV1<{ deleted: string; item: CatalogItem }>("/api/v1/installs/delete", { install_id: installId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Upload a SKILL.md or a zip of a skill or plugin directory; `name` renames it. Installed nowhere; 409 when the
 * name is taken. `skipped` lists the binary files left out. */
export async function uploadCatalogItem(upload: {
  filename: string;
  content_base64: string;
  name?: string;
}): Promise<{ item: CatalogItem; skipped: LibrarySkipped[] }> {
  const result = await callV1<{ item: CatalogItem; skipped: LibrarySkipped[] }>("/api/v1/catalog_sources/upload", upload);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
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
export async function previewGitRepo(source: GitSource): Promise<GitPreview> {
  const result = await callV1<GitPreview>("/api/v1/catalog_sources/git_preview", source);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Import the chosen items of a previewed repository; 409 when a name is taken or the ref moved since `commit`. */
export async function importGitRepo(
  source: GitSource & { commit: string; items: { path: string; name?: string }[] },
): Promise<GitImport> {
  const result = await callV1<GitImport>("/api/v1/catalog_sources/git_import", source);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Write a skill in Settings (origin "ui"); installed nowhere. */
export async function createCatalogSkill(skill: NewProjectSkill): Promise<{ item: CatalogItem }> {
  const result = await callV1<{ item: CatalogItem }>("/api/v1/catalog_sources/create_skill", skill);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Change a Settings, uploaded or git skill; an uploaded or git one is marked modified. */
export async function updateCatalogItem(itemId: string, fields: Partial<NewProjectSkill>): Promise<{ item: CatalogItem }> {
  const result = await callV1<{ item: CatalogItem }>("/api/v1/catalog_items/update", { item_id: itemId, fields });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** What applies to a project: its own installs and the inherited global ones. */
export async function fetchProjectInstalls(projectId: string): Promise<ProjectInstalls> {
  const result = await callV1<ProjectInstalls>("/api/v1/installs/project", { project_id: projectId });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
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
  /** The Manager this channel talks to. */
  manager_id: string;
  manager_name: string | null;
  /** One of the three built-in rows (the general Manager's), as opposed to an added bot. */
  builtin: boolean;
  created_at: string;
  updated_at: string;
}

/** A Lark channel's saved settings (`channels.status`). */
export interface LarkStatus {
  /** The channel's bot; profile is empty for lark_bot, and bot is null when unavailable (see `problem`). */
  bot: { name: string; app_id: string; open_id: string; profile: string } | null;
  /** Why the channel would not start (credentials, lark-cli, or an allowlist for another app); null otherwise. */
  problem: string | null;
  /** The app the sender allowlist was made for; open_ids differ per app. */
  allowlist_app_id: string | null;
  /** Names of the allowlist's and the refused senders' open_ids, where the channel knows them. */
  names: Record<string, string>;
  /** The latest senders the allowlist refused, newest first, not on the saved allowlist; `chat_id` is the group
   * chat they last @-mentioned the bot in, null for a direct message. */
  rejected: { open_id: string; name: string | null; count: number; last_at: string; chat_id: string | null }[];
}

/** A person the people search found: their open_id for the channel's app. */
export interface LarkPerson {
  open_id: string;
  name: string;
  email: string;
  department: string;
}

export async function fetchLarkStatus(channelType = "lark", channelId?: string): Promise<LarkStatus> {
  const result = await callV1<LarkStatus>("/api/v1/channels/status", { channel_type: channelType, ...(channelId ? { channel_id: channelId } : {}) });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function searchLarkPeople(query: string, channelType = "lark", channelId?: string): Promise<{ people: LarkPerson[]; complete: boolean }> {
  const result = await callV1<{ people: LarkPerson[]; complete: boolean }>("/api/v1/channels/people", {
    channel_type: channelType, ...(channelId ? { channel_id: channelId } : {}), q: query,
  });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
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

export async function createChannelBot(body: {
  channel_type: string;
  manager_id: string;
  display_name?: string;
  enabled?: boolean;
  config?: Record<string, unknown>;
  bot_token?: string;
}): Promise<ChannelConfig> {
  const res = await fetch(`${API_BASE}/channel-configs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) await failed(res, "add bot");
  return res.json();
}

export async function deleteChannelBot(channelId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/channel-configs/${encodeURIComponent(channelId)}`, { method: "DELETE" });
  if (!res.ok) await failed(res, "remove bot");
}

/** A chat of a channel: where a dashboard task's gate messages also go. */
export interface GateTarget {
  channel: GateChannel;
  chat: string;
}

/** A chat a channel's sender allowlist lets in (the same list as a schedule's notify targets). */
export interface GateTargetOption extends GateTarget {
  label: string;
  channel_enabled: boolean;
}

/** Where the gate messages of tasks started on the dashboard go besides the task page; null: only the task page. */
export interface GateSettings {
  dashboard_target: GateTarget | null;
  /** Set when the saved chat is no longer on its channel's sender allowlist: gates then go to the task page only. */
  dashboard_target_problem?: string | null;
  targets: GateTargetOption[];
}

export async function fetchGateSettings(): Promise<GateSettings> {
  const result = await callV1<GateSettings>("/api/v1/gate_settings/get", {});
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** Store the dashboard tasks' gate target; 400 for a chat that is not one of `targets`. */
export async function updateGateSettings(dashboardTarget: GateTarget | null): Promise<GateSettings> {
  const result = await callV1<GateSettings>("/api/v1/gate_settings/update", { dashboard_target: dashboardTarget });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export interface VaultSettings {
  configured: boolean;
  path: string | null;
  writable: string[];
  rules_file: string | null;
  git: boolean;
  problem: string | null;
}

export async function fetchVaultSettings(): Promise<VaultSettings> {
  const res = await fetch(`${API_BASE}/knowledge-vault`);
  if (!res.ok) await failed(res, "knowledge vault settings");
  return res.json();
}

export async function updateVaultSettings(body: {
  path: string | null;
  writable?: string[];
  rules_file?: string | null;
}): Promise<VaultSettings> {
  const res = await fetch(`${API_BASE}/knowledge-vault`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) await failed(res, "save knowledge vault settings");
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
        if (done || abortController.signal.aborted) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        let eventType = "";
        let eventData = "";

        for (const line of lines) {
          if (abortController.signal.aborted) break;
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
      if ((e as Error).name !== "AbortError" && !(e instanceof SessionEndedError)) {
        console.error("SSE error:", e);
      }
    } finally {
      onDone?.();
    }
  })();

  return () => abortController.abort();
}

/** Which gates `gates.list` lists: waiting ones, closed ones (passed, cancelled, skipped) or all. */
export type GateListState = "open" | "closed" | "all";

/** One row of `gates.list`: a gate, its task, and what the list shows beside it. */
export interface GateListRow {
  task: {
    task_id: string;
    name: string;
    status: string;
    group: TaskGroup;
    project_id: string;
    project_name: string;
  };
  gate: UserGate;
  /** The agenda questions still open. */
  agenda_open: number;
  /** The last user or Planner message; null for none. */
  last_message: { role: string; author: string | null; via: string; created_at: string } | null;
}

export interface GateList {
  gates: GateListRow[];
  /** The rows under the filter. */
  total: number;
  /** Counts under the project filter only. */
  counts: { open: number; waiting_user: number; waiting_planner: number; closed: number; all: number };
  limit: number;
  offset: number;
  next_cursor: string | null;
  has_more: boolean;
}

export async function fetchGates(
  filters: {
    state?: GateListState;
    waitingOn?: "user" | "planner";
    projectId?: string;
    limit?: number;
    offset?: number;
    cursor?: string;
    through?: string;
  } = {},
): Promise<GateList> {
  const limit = filters.limit ?? 50;
  const offset = filters.offset ?? 0;
  if (filters.cursor && offset) throw new Error("cursor and offset cannot be combined");
  if (!Number.isInteger(limit) || limit < 1 || limit > 200 || !Number.isInteger(offset) || offset < 0)
    throw new Error("Invalid gate list limit or offset");
  const request = async (at: number): Promise<GateList> => {
    const result = await callV1<GateList>("/api/v1/ops/gates/list", {
      ...(filters.state ? { state: filters.state } : {}),
      ...(filters.waitingOn ? { waiting_on: filters.waitingOn } : {}),
      ...(filters.projectId ? { project_id: filters.projectId } : {}), limit: 200, offset: at,
    });
    if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
    return result;
  };
  // The Tool accepts offsets only. Read its complete ordered list and keep opaque boundaries for dashboard refreshes;
  // using the server's order also preserves PostgreSQL's collation when opened_at timestamps tie.
  const list = await request(0);
  const rows = [...list.gates];
  let page = list;
  while (page.has_more) {
    if (page.gates.length === 0 || rows.length >= list.total)
      throw new Error("The gate list changed while loading. Retry the request.");
    page = await request(rows.length);
    if (page.total !== list.total) throw new Error("The gate list changed while loading. Retry the request.");
    rows.push(...page.gates);
  }
  const keys = rows.map((row) => JSON.stringify([row.task.task_id, row.gate.step]));
  if (rows.length !== list.total || new Set(keys).size !== keys.length)
    throw new Error("The gate list changed while loading. Retry the request.");
  const positions = new Map(keys.map((key, index) => [key, index]));
  const boundary = (raw: string, side: "after" | "through"): number => {
    let saved: { keys: string[]; end: number };
    try { saved = JSON.parse(raw); } catch { throw new Error("Invalid gate list cursor"); }
    if (!saved || !Array.isArray(saved.keys) || !saved.keys.every((key) => typeof key === "string")
      || !Number.isInteger(saved.end) || saved.end < 0 || saved.end > saved.keys.length)
      throw new Error("Invalid gate list cursor");
    const known = side === "after" ? saved.keys.slice(0, saved.end).reverse() : saved.keys.slice(saved.end);
    for (const key of known) {
      const index = positions.get(key);
      if (index !== undefined) return side === "after" ? index + 1 : index;
    }
    const opposite = side === "after" ? saved.keys.slice(saved.end) : saved.keys.slice(0, saved.end).reverse();
    for (const key of opposite) {
      const index = positions.get(key);
      if (index !== undefined) return side === "after" ? index : index + 1;
    }
    if (rows.length === 0) return 0;
    // With no surviving boundary, a refresh starts over with the current list.
    if (side === "through") return rows.length;
    throw new Error("The gate list changed while loading. Retry the request.");
  };
  const start = filters.cursor ? boundary(filters.cursor, "after") : offset;
  const stop = filters.through ? boundary(filters.through, "through") : rows.length;
  const gates = rows.slice(start, Math.max(start, Math.min(start + limit, stop)));
  const end = start + gates.length;
  return { ...list, gates, limit, offset, has_more: end < stop,
    next_cursor: gates.length ? JSON.stringify({ keys, end }) : null };
}

// --- Managers: each with its own duty, tools, task visibility and task templates ---

export type ManagerTools = "all" | "tasks";
export type TaskScope = "all" | "own";
export type TaskPolicy = "free" | "templates_only";

export interface TemplateInput {
  key: string;
  label: string;
  kind: "text" | "url";
  required: boolean;
  pattern: string;
  hint: string;
}

export interface TaskTemplate {
  key: string;
  name: string;
  description: string;
  project_id: string | null;
  work_dir: string;
  workspace_mode: "worktree" | "in_place" | null;
  requirement: string;
  planner_instructions: string;
  profiles: Partial<Record<"planner" | "worker" | "reviewer", string>>;
  role_templates: string[];
  inputs: TemplateInput[];
  updated_at: string;
}

export type TaskTemplateFields = Omit<TaskTemplate, "key" | "updated_at">;

export interface ManagerInfo {
  id: string;
  name: string;
  description: string;
  builtin: boolean;
  enabled: boolean;
  tools: ManagerTools;
  task_scope: TaskScope;
  task_policy: TaskPolicy;
  system_prompt: string | null;
  profile_id: string | null;
  profile_name: string | null;
  templates: TaskTemplate[];
  revision: number;
  created_at: string;
  updated_at: string;
  state: { running: boolean; busy: boolean; healthy: boolean; sync: "ok" | "pending"; sync_error?: string };
  spend: { today_usd: number; last_7d_usd: number; runs_today: number };
}

export interface ManagerFields {
  key?: string;
  name?: string;
  description?: string;
  system_prompt?: string | null;
  profile_id?: string | null;
  tools?: ManagerTools;
  task_scope?: TaskScope;
  task_policy?: TaskPolicy;
  enabled?: boolean;
}

async function managerJson<T>(url: string, method: string, body: unknown | undefined, label: string): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) await failed(res, label);
  return res.json();
}

/** A token for an agent or script that uses Looper through loopercli; the token itself is never listed. */
export interface ApiClient {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
}

export async function fetchApiClients(): Promise<ApiClient[]> {
  const result = await callV1<{ clients: ApiClient[] }>("/api/v1/clients/list", {});
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result.clients;
}

/** Add a client; the answer holds its token, which the server shows once and does not keep. */
export async function createApiClient(name: string): Promise<{ client: ApiClient; token: string }> {
  const result = await callV1<{ client: ApiClient; token: string }>("/api/v1/clients/create", { name });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function revokeApiClient(id: string): Promise<void> {
  const result = await callV1<{ revoked: string }>("/api/v1/clients/revoke", { client_id: id });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
}

type ManagerView = Omit<ManagerInfo, "state" | "spend"> & Partial<Pick<ManagerInfo, "state" | "spend">>;
type ManagersSnapshot = { managers: ManagerView[]; max_enabled: number };

export async function fetchManagers(): Promise<ManagerInfo[]> {
  const result = await callV1<{ managers: ManagerInfo[]; max_enabled: number }>("/api/v1/managers/list", {});
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result.managers;
}

export async function createManager(body: ManagerFields): Promise<{ manager: ManagerView }> {
  const result = await callV1<{ manager: ManagerView }>("/api/v1/managers/create", body);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function updateManager(id: string, body: ManagerFields): Promise<{ manager: ManagerView }> {
  const result = await callV1<{ manager: ManagerView }>("/api/v1/managers/update", { manager_id: id, fields: body });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function deleteManager(id: string): Promise<ManagersSnapshot> {
  const result = await callV1<ManagersSnapshot>("/api/v1/managers/delete", { manager_id: id });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function putManagerTemplate(id: string, key: string, body: TaskTemplateFields): Promise<ManagersSnapshot> {
  const result = await callV1<ManagersSnapshot>("/api/v1/manager_templates/put", { manager_id: id, key, ...body });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export async function deleteManagerTemplate(id: string, key: string): Promise<ManagersSnapshot> {
  const result = await callV1<ManagersSnapshot>("/api/v1/manager_templates/delete", { manager_id: id, key });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}
