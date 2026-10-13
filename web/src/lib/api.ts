const API_BASE = "/api";

export interface SystemStatus {
  status: string;
  agents: Record<string, { healthy: boolean; agent_id: string }>;
  channels: Record<string, unknown>;
  uptime: number;
}

export interface TaskInfo {
  task_id: string;
  requirement: string;
  goal_id: string;
  status: string;
  phase: string;
  planner_id: string | null;
  correlation_id: string;
  created_at: string;
  completed_at: string | null;
  /** Every task belongs to exactly one project. */
  project_id: string;
  project_name: string;
  work_dir: string;
}

/** The most characters a project background may have. */
export const BACKGROUND_MAX_CHARS = 10000;

/** The most characters a harness prompt (the default, the Manager's or a project's) may have. */
export const HARNESS_MAX_CHARS = 20000;

/** A group of tasks with an optional default work directory and background. */
export interface Project {
  project_id: string;
  name: string;
  description: string;
  /** Default work directory for new tasks; "" = none. */
  work_dir: string;
  /** Added to the system prompt of the project's Planner, Workers and Reviewers; "" = none. */
  background: string;
  /** The harness prompt of the project's Planner; "" = the default one in Settings. */
  planner_harness_prompt: string;
  /** The harness prompt of the project's Workers and Reviewers; "" = the default one in Settings. */
  worker_harness_prompt: string;
  created_at: string;
  updated_at: string;
  task_count: number;
  /** Tasks pending, assigned, running or executing. */
  active_count: number;
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

export async function fetchTasks(projectId?: string): Promise<TaskInfo[]> {
  const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  const res = await fetch(`${API_BASE}/tasks${qs}`);
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

export interface NewProjectFields {
  name: string;
  description?: string;
  /** Absolute default work directory; omit or "" for none. */
  work_dir?: string;
  /** At most BACKGROUND_MAX_CHARS characters; omit or "" for none. */
  background?: string;
  /** At most HARNESS_MAX_CHARS characters each; omit or "" to inherit the default harness. */
  planner_harness_prompt?: string;
  worker_harness_prompt?: string;
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
export const PROJECTS_CHANGED_EVENT = "loopx:projects-changed";

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

/** The project fields an update can change. */
export type ProjectUpdate = Partial<Omit<NewProjectFields, "name">> & { name?: string };

/** Send only the fields to change; `work_dir: ""`, `background: ""` and an empty harness clear them. */
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
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  steps: TaskStep[];
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

// --- Agent configs ---

export interface AgentConfig {
  agent_id: string;
  display_name: string;
  role: string;
  runtime: string;
  llm_profile_id: number | null;
  enabled: boolean;
  /** The role prompt: what this kind of agent is responsible for. */
  system_prompt: string;
  /** The system-recommended prompt; "Reset to recommended" copies it into system_prompt. */
  default_prompt: string;
  /** The Manager's own harness prompt; "" inherits the default. Other agents refuse it (their harness is per project). */
  harness_prompt: string;
  /** This agent's model; null or "" uses the default model in Settings. */
  model: string | null;
  max_turns: number;
  timeout_seconds: number;
  max_concurrency: number;
  permission_mode: string | null;
  extra_args: Record<string, unknown> | null;
  metadata: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

/** One Claude run of an agent (Manager, Worker or reviewer). */
export type HarnessSource = "builtin_default" | "system_default" | "manager_override" | "project_override";
export type ModelSource = "builtin_default" | "system_default" | "agent_override";

/** Where each layer of a run's system prompt came from (absent on runs recorded before it existed). */
export interface PromptLayers {
  harness?: { source: HarnessSource; sha256: string; project_id?: string } | null;
  role?: { config_id: string; sha256: string; recommended: boolean };
  project?: { id: string; background_sha256: string | null; updated_at: string | null } | null;
  task?: { kind: "plan" | "work" | "review"; task_id: string | null; step_id: string | null; sha256: string } | null;
  contract?: { name: string; sha256: string };
  /** The flag set that kept the operator's personal Claude Code configuration out of the run. */
  isolation?: string;
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
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  duration_ms: number | null;
  model: string | null;
  error: string | null;
  metadata: {
    command?: string[];
    cwd?: string;
    /** What LoopX appended to Claude Code's system prompt (its built-in prompt is not included). */
    system_prompt?: string;
    system_prompt_sha256?: string;
    prompt_layers?: PromptLayers;
    /** The model asked for (an alias or full name) and where it came from. */
    model_choice?: { model: string; source: ModelSource; effort: string | null };
    /** The environment variables from Settings: their names, with each value's sha256. */
    env?: { sha256: string; keys: Record<string, string> };
  } | null;
  created_at: string;
  finished_at: string | null;
  /** Heartbeat: last time the run produced any output. */
  last_output_at: string | null;
}

/** One stream-json event of a run, in order. */
export interface LlmRecordEvent {
  seq: number;
  event: Record<string, unknown>;
  created_at: string;
}

export async function fetchAgentConfigs(): Promise<AgentConfig[]> {
  const res = await fetch(`${API_BASE}/agent-configs`);
  if (!res.ok) throw new Error(`agent-configs ${res.status}`);
  const data = await res.json();
  return data.configs ?? [];
}

export async function fetchAgentConfig(agentId: string): Promise<AgentConfig> {
  const res = await fetch(`${API_BASE}/agent-configs/${agentId}`);
  if (!res.ok) throw new Error(`agent-config ${res.status}`);
  return res.json();
}

export async function updateAgentConfig(
  agentId: string,
  fields: Partial<AgentConfig>,
): Promise<AgentConfig> {
  const res = await fetch(`${API_BASE}/agent-configs/${agentId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update agent-config");
  return res.json();
}

/** Reset an agent's system prompt to the system-recommended default. */
export async function resetAgentPrompt(agentId: string): Promise<AgentConfig> {
  const res = await fetch(`${API_BASE}/agent-configs/${agentId}/reset-prompt`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(`reset agent-config ${res.status}`);
  return res.json();
}

export async function fetchLlmRecords(params: {
  agent_id?: string;
  task_id?: string;
  /** An agent_configs id: "manager", "planner" (each task's Planner), "worker" or "reviewer". */
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

// --- LLM profiles ---

export interface LlmProfile {
  id: number;
  name: string;
  display_name: string;
  provider_kind: string;
  model: string;
  base_url: string | null;
  api_key_env: string | null;
  reasoning_effort: string | null;
  max_tokens: number | null;
  context_window_k: number | null;
  rate_per_minute: number | null;
  streaming: boolean;
  is_default: boolean;
  priority: number;
  description: string;
  metadata: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export async function fetchLlmProfiles(): Promise<LlmProfile[]> {
  const res = await fetch(`${API_BASE}/llm-profiles`);
  if (!res.ok) throw new Error(`llm-profiles ${res.status}`);
  const data = await res.json();
  return data.profiles ?? [];
}

export async function createLlmProfile(
  fields: Partial<LlmProfile>,
): Promise<LlmProfile> {
  const res = await fetch(`${API_BASE}/llm-profiles`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) throw new Error(`create llm-profile ${res.status}`);
  return res.json();
}

export async function updateLlmProfile(
  profileId: number,
  fields: Partial<LlmProfile>,
): Promise<LlmProfile> {
  const res = await fetch(`${API_BASE}/llm-profiles/${profileId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) throw new Error(`update llm-profile ${res.status}`);
  return res.json();
}

export async function deleteLlmProfile(profileId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/llm-profiles/${profileId}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(`delete llm-profile ${res.status}`);
}

// --- Agent settings: what every agent inherits ---

export interface EnvEntry {
  key: string;
  value: string;
}

export interface AgentSettings {
  /** The default harness every agent inherits unless the Manager or a project overrides it. */
  default_harness_prompt: string;
  harness_source: "builtin_default" | "system_default";
  builtin_harness_prompt: string;
  harness_max_chars: number;
  default_model: string;
  model_source: "builtin_default" | "system_default";
  builtin_model: string;
  /** "" = Claude Code's default effort. */
  default_effort: string;
  effort_levels: string[];
  /** Non-secret variables added to every agent's environment. */
  default_env: EnvEntry[];
  env_max_entries: number;
}

export type AgentSettingsUpdate = Partial<
  Pick<AgentSettings, "default_harness_prompt" | "default_model" | "default_effort" | "default_env">
>;

export async function fetchAgentSettings(): Promise<AgentSettings> {
  const res = await fetch(`${API_BASE}/settings/agents`);
  if (!res.ok) await failed(res, "agent settings");
  return res.json();
}

/** Send only the fields to change; an empty value goes back to the built-in default. */
export async function updateAgentSettings(fields: AgentSettingsUpdate): Promise<AgentSettings> {
  const res = await fetch(`${API_BASE}/settings/agents`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) await failed(res, "update agent settings");
  return res.json();
}

export async function resetDefaultHarness(): Promise<AgentSettings> {
  const res = await fetch(`${API_BASE}/settings/agents/reset-harness`, { method: "POST" });
  if (!res.ok) await failed(res, "reset harness");
  return res.json();
}

// --- Channel configs ---

export interface ChannelConfig {
  channel_id: string;
  channel_type: string;
  display_name: string;
  enabled: boolean;
  config: Record<string, string>;
  created_at: string;
  updated_at: string;
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
  fields: Partial<ChannelConfig>,
): Promise<ChannelConfig> {
  const res = await fetch(`${API_BASE}/channel-configs/${channelId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
  if (!res.ok) throw new Error(`update channel-config ${res.status}`);
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
