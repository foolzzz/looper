import { callV1, V1Error } from "@/lib/v1";

/** The sections of the "needs you" summary (`attention.summary`), in the order the server lists them. */
export type AttentionKind = "gates" | "confirmations" | "schedules" | "logins" | "intents" | "ledger" | "deliveries" | "cost" | "failure_holds" | "reminders" | "vault_commits";

/** One thing that waits for the operator. `facts` are the kind's own (`looper/agents/attention.py`). */
export interface AttentionItem {
  key: string;
  title: string;
  /** Where it belongs: a task, a project, a Manager or an intent type; "" when nothing. */
  context: string;
  since: string | null;
  /** Where the dashboard shows it; null when no page does. */
  href: string | null;
  /** What a chat calls it (I-0042, L-0012); null when nothing. */
  ref: string | null;
  facts: Record<string, unknown>;
}

/** `ok`: read (maybe empty); `error`: its source could not be read; `unknown`: this server cannot tell. */
export interface AttentionSection {
  kind: AttentionKind;
  state: "ok" | "error" | "unknown";
  count: number;
  items: AttentionItem[];
  error: string | null;
}

export interface AttentionSummary {
  read_at: string;
  /** What the readable sections hold. */
  total: number;
  sections: AttentionSection[];
}

/** Read the feature setting without collecting a summary or provoking an off-state 409. */
export async function attentionSettingsGet(): Promise<{ enabled: boolean }> {
  const result = await callV1<{ enabled: boolean }>("/api/v1/attention/settings_get", {});
  if ("status" in result && result.status === "needs_confirmation")
    throw new V1Error(202, "unavailable", "The server asked to confirm a read.");
  return result as { enabled: boolean };
}

/** The summary now; a 409 `V1Error` while `attention.enabled` is off. */
export async function fetchAttention(signal?: AbortSignal): Promise<AttentionSummary> {
  const result = await callV1<AttentionSummary>("/api/v1/attention/summary", {}, { signal });
  if ("status" in result && result.status === "needs_confirmation")
    throw new V1Error(202, "unavailable", "The server asked to confirm a read.");
  return result as AttentionSummary;
}

/** Whether an error says the summary is off. */
export const isAttentionOff = (error: unknown) => error instanceof V1Error && error.status === 409;
