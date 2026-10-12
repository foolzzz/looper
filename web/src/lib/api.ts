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

export async function fetchTasks(): Promise<TaskInfo[]> {
  const res = await fetch(`${API_BASE}/tasks`);
  if (!res.ok) throw new Error(`tasks ${res.status}`);
  const data = await res.json();
  return data.tasks ?? data;
}

/** Who a chat message goes to: the global Manager or a running task's Planner. */
export type ChatTarget = { kind: "manager" } | { kind: "planner"; task_id: string };

export interface ChatTargetInfo {
  kind: "manager" | "planner";
  id: string;
  task_id?: string;
  requirement?: string;
  status?: string;
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
}

export async function stopTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/stop`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  });
  if (!res.ok) throw new Error(`stop ${res.status}`);
}

export async function startTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  });
  if (!res.ok) throw new Error(`start ${res.status}`);
}

export async function createTask(params: {
  requirement: string;
  work_dir?: string;
  config?: Record<string, unknown>;
  goal_id?: string;
  project_root?: string;
}): Promise<{ task_id: string; status: string; created_at: string }> {
  const res = await fetch(`${API_BASE}/tasks/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!res.ok) throw new Error(`create task ${res.status}`);
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
  if (!res.ok) throw new Error(`update task config ${res.status}`);
}

export interface TaskDetail extends TaskInfo {
  project_root: string;
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
  system_prompt: string;
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

export interface LlmRecord {
  id: number;
  agent_id: string;
  task_id: string | null;
  session_id: string | null;
  input_summary: string | null;
  output_summary: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  model: string | null;
  error: string | null;
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
  if (!res.ok) throw new Error(`update agent-config ${res.status}`);
  return res.json();
}

export async function fetchLlmRecords(params: {
  agent_id?: string;
  task_id?: string;
  limit?: number;
  offset?: number;
}): Promise<LlmRecord[]> {
  const qs = new URLSearchParams();
  if (params.agent_id) qs.set("agent_id", params.agent_id);
  if (params.task_id) qs.set("task_id", params.task_id);
  if (params.limit) qs.set("limit", String(params.limit));
  if (params.offset) qs.set("offset", String(params.offset));
  const res = await fetch(`${API_BASE}/llm-records?${qs}`);
  if (!res.ok) throw new Error(`llm-records ${res.status}`);
  const data = await res.json();
  return data.records ?? [];
}

// --- Agent sessions ---

export interface AgentSession {
  task_id: string;
  turn_count: number;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  total_duration_ms: number | null;
  first_at: string;
  last_at: string;
  requirement: string;
  task_status: string;
  task_phase: string;
}

export interface SessionMessage {
  id: number;
  input_summary: string | null;
  output_summary: string | null;
  input_text: string | null;
  output_text: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  duration_ms: number | null;
  model: string | null;
  error: string | null;
  created_at: string;
}

export async function fetchAgentSessions(agentId: string): Promise<AgentSession[]> {
  const res = await fetch(`${API_BASE}/agents/${agentId}/sessions`);
  if (!res.ok) throw new Error(`agent sessions ${res.status}`);
  const data = await res.json();
  return data.sessions ?? [];
}

export async function fetchSessionMessages(
  agentId: string,
  taskId: string,
): Promise<{ messages: SessionMessage[]; activity: ActivityEvent[] }> {
  const res = await fetch(`${API_BASE}/agents/${agentId}/sessions/${taskId}/messages`);
  if (!res.ok) throw new Error(`session messages ${res.status}`);
  return res.json();
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
