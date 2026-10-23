import { isSessionEnded, SessionEndedError, subscribeSessionEnded } from "@/lib/session";
import { randomId } from "@/lib/ids";

export interface V1ErrorDetails {
  manager_id?: string;
  state?: string;
  phase?: string | null;
  method?: string | null;
}

export class V1Error extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: V1ErrorDetails;

  constructor(status: number, code: string, message: string, details?: V1ErrorDetails) {
    super(message);
    this.name = "V1Error";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export interface V1PendingConfirmation {
  status: "needs_confirmation";
  confirmation_id: string;
  expires_in_seconds: number;
  impact: string;
  /** Keep this action's session when explicitly confirming it. */
  session: string;
}

export interface V1Options { signal?: AbortSignal; session?: string }

function request(body: unknown, { signal, session }: V1Options): RequestInit {
  session ??= randomId();
  return { method: "POST", credentials: "same-origin", signal,
    headers: { "Content-Type": "application/json", Accept: "application/json", "X-Looper-Session": session },
    body: JSON.stringify(body) };
}

async function failure(response: Response): Promise<never> {
  const body = await response.json().catch(() => null);
  const error = body?.status === "error" ? body.error : null;
  throw new V1Error(response.status, error?.code ?? "unavailable", error?.message ?? "The request failed.", error?.details);
}

async function pendingConfirmation(response: Response, init: RequestInit): Promise<V1PendingConfirmation> {
  const answer = await response.json().catch(() => null);
  if (answer?.status !== "needs_confirmation") throw new V1Error(202, "invalid", "The server returned an invalid confirmation response.");
  return { ...answer, session: (init.headers as Record<string, string>)["X-Looper-Session"] };
}

/** A pending answer is a value, never a completed result or an automatic confirmation. */
export async function callV1<T>(path: string, body: unknown, options: V1Options = {}): Promise<T | V1PendingConfirmation> {
  if (isSessionEnded()) throw new SessionEndedError();
  const init = request(body, options);
  const response = await fetch(path, init);
  if (!response.ok) return failure(response);
  if (response.status === 202) return pendingConfirmation(response, init);
  const answer = await response.json();
  if (answer.status === "ok") return answer.result as T;
  if (answer.status === "error") throw new V1Error(response.status, answer.error.code, answer.error.message, answer.error.details);
  throw new V1Error(response.status, "unavailable", "The server returned an invalid operation response.");
}

export function confirmV1<T>(id: string, session: string, signal?: AbortSignal): Promise<T | V1PendingConfirmation> {
  return callV1<T>("/api/v1/confirm", { confirmation_id: id }, { session, signal });
}

/** POST SSE; the caller owns cancellation and receives errors, including the session guard's 401. */
export async function streamV1<T>(path: string, body: unknown, onEvent: (event: { event: string; data: T }) => void,
  options: V1Options = {}): Promise<void | V1PendingConfirmation> {
  if (isSessionEnded()) throw new SessionEndedError();
  const init = request(body, options);
  (init.headers as Record<string, string>).Accept = "text/event-stream";
  const response = await fetch(path, init);
  if (!response.ok) return failure(response);
  if (response.status === 202) return pendingConfirmation(response, init);
  if (response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "text/event-stream") return failure(response);
  if (!response.body) throw new V1Error(response.status, "unavailable", "The server returned no event stream.");
  const reader = response.body.getReader();
  const cancel = () => { void reader.cancel().catch(() => {}); };
  const unsubscribe = subscribeSessionEnded(cancel);
  options.signal?.addEventListener("abort", cancel, { once: true });
  const decoder = new TextDecoder();
  let buffer = "", event = "message", data: string[] = [];
  try {
    while (true) {
      options.signal?.throwIfAborted();
      if (isSessionEnded()) throw new SessionEndedError();
      const chunk = await reader.read();
      options.signal?.throwIfAborted();
      if (isSessionEnded()) throw new SessionEndedError();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const raw of lines) {
        options.signal?.throwIfAborted();
        if (isSessionEnded()) throw new SessionEndedError();
        const line = raw.replace(/\r$/, "");
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
        else if (!line) {
          if (data.length) onEvent({ event, data: JSON.parse(data.join("\n")) as T });
          event = "message"; data = [];
        }
      }
    }
  } finally {
    unsubscribe();
    options.signal?.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
