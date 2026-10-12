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
  planner_id: string | null;
  correlation_id: string;
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
  return res.json();
}

export async function sendChat(text: string): Promise<ChatSendResponse> {
  const res = await fetch(`${API_BASE}/chat/send`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) throw new Error(`chat send ${res.status}`);
  return res.json();
}

export async function cancelTask(taskId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/tasks/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_id: taskId }),
  });
  if (!res.ok) throw new Error(`cancel ${res.status}`);
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
