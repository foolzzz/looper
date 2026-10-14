const API_BASE = "/api";

export interface SystemStatus {
  status: string;
  agents: Record<string, { healthy: boolean; agent_id: string }>;
  channels: Record<string, unknown>;
  uptime: number;
  /** The startup self-check, as `loopx doctor` prints it. */
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
  /** What the task's Planner, Workers and Reviewers spent so far; null when it could not be read. */
  cost?: TaskCost | null;
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
  /** Finished runs that ended without a reported cost (Claude exited first). */
  unpriced_runs: number;
  by_role: Record<TaskRole, { cost_usd: number; runs: number }>;
}

/** The most characters a project background may have. */
export const BACKGROUND_MAX_CHARS = 10000;

/** The most characters a harness prompt (the default, the Manager's or a project's) may have. */
export const HARNESS_MAX_CHARS = 20000;

/** How a project's Planner, Workers and Reviewers run; null inherits today's behavior. */
export interface ProjectExecution {
  /** Their model, over the agent's own and the default in Settings. */
  model: string | null;
  /** `--max-budget-usd` on each of their Claude Code processes. */
  run_budget_usd: number | null;
  /** The most one task may spend over all its runs. */
  task_budget_usd: number | null;
  /** At the task cap: fail at once (also when null) or after the Planner sums up. */
  task_budget_action: "wrap_up" | "fail" | null;
  /** How often the Planner checks running Workers; 0 = never. */
  supervise_interval_seconds: number | null;
  /** "planner": the Planner decides which steps are reviewed; "all": every step is. */
  review_policy: "planner" | "all" | null;
  /** true: a task that completes leaves a lessons document in the project's knowledge; null or false: off. */
  distill_lessons: boolean | null;
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

/** The project fields an update can change; an execution setting set to null inherits again. */
export type ProjectUpdate = Partial<Omit<NewProjectFields, "name">> & { name?: string } & {
  [K in keyof ProjectExecution]?: ProjectExecution[K] | string;
};

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
  /** The project's spend cap per task (USD); null for none. */
  task_budget_usd: number | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  steps: TaskStep[];
  /** The task journal, oldest first: an entry per accepted step. */
  journal?: JournalEntry[];
}

/** What an accepted step delivered and what its Worker and Reviewer found. */
export interface JournalEntry {
  id: number;
  step_id: string;
  /** The Planner's label for the step; "" when the step is gone. */
  step_key: string;
  kind: "step_accepted";
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
  /** The run's own tokens; for a resident agent's turn, that turn's share of its session. */
  input_tokens: number | null;
  output_tokens: number | null;
  /** Prompt tokens read from the cache and written to it. */
  cache_read_tokens?: number | null;
  cache_creation_tokens?: number | null;
  /** The run's own cost; for a resident agent's turn, that turn's share of its session. */
  cost_usd: number | null;
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
    /** What LoopX appended to Claude Code's system prompt (its built-in prompt is not included). */
    system_prompt?: string;
    system_prompt_sha256?: string;
    prompt_layers?: PromptLayers;
    /** The model asked for (an alias or full name) and where it came from. */
    model_choice?: { model: string; source: ModelSource; effort: string | null };
    /** The environment variables from Settings: their names, with each value's sha256. */
    env?: { sha256: string; keys: Record<string, string> };
    /** The catalog items installed for the run (plugins and skills) and their hash; error when it went without
     * them. */
    library?: RunLibrary;
    /** Runs recorded before the catalog: the project skills a task agent loaded. */
    project_skills?: RunProjectSkills;
  } | null;
  created_at: string;
  finished_at: string | null;
  /** Heartbeat: last time the run produced any output. */
  last_output_at: string | null;
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

// --- The capability catalog: items acquired into LoopX, and where they are installed ---

export type LibraryKind = "plugin" | "skill";
/** How an item was acquired: imported from this machine, uploaded, taken from git, written in Settings ("ui") or in a
 * project. */
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
  /** Where it is installed; empty when it loads nowhere. */
  installs: Install[];
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

/** Remove a Settings, uploaded or git item from the catalog, with its installs. */
export function deleteCatalogItem(itemId: string): Promise<{ deleted: string }> {
  return catalogRequest("DELETE", `/${encodeURIComponent(itemId)}`, "delete");
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
