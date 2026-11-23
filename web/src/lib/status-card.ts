import { callV1, V1Error } from "@/lib/v1";

/** The `status_card.get` snapshot (assistant phase 3, 3.7a), only the fields the dashboard card renders. Fields a
 * newer server adds are ignored; the ones that may be missing are optional. */
export type StatusSectionState = "ok" | "error" | "unknown";

export interface StatusSession {
  state: "idle" | "answering" | "background";
  origin?: string | null;
  channel?: string | null;
  chat?: string | null;
  group?: boolean;
  client_id?: string | null;
  elapsed_seconds?: number | null;
  queued?: number;
}

export interface StatusManager { manager_id: string; name?: string | null; queued: number; sessions: StatusSession[] }
export interface StatusWaitingKind { kind: string; state: StatusSectionState; count: number; error?: string | null }

interface SectionBase { state: StatusSectionState; error?: string | null }
export type StatusSection =
  | (SectionBase & { name: "manager"; managers?: StatusManager[] })
  | (SectionBase & { name: "tasks"; count?: number })
  | (SectionBase & { name: "schedules"; next?: { name: string; next_fire_at?: string | null; timezone?: string | null }[]; running?: { run_id: string; name?: string }[] })
  | (SectionBase & { name: "waiting"; kinds?: StatusWaitingKind[] })
  | (SectionBase & { name: "reminders"; next?: { due_at?: string | null; text: string; timezone?: string | null } | null })
  | (SectionBase & { name: string });

export interface StatusSnapshot { read_at: string; text?: string; sections: StatusSection[] }

/** The card is off (`status_card.enabled`): the operation answers HTTP 409. */
export const isStatusCardOff = (error: unknown) => error instanceof V1Error && error.status === 409;

export async function fetchStatusCard(signal?: AbortSignal): Promise<StatusSnapshot> {
  const result = await callV1<StatusSnapshot>("/api/v1/status_card/get", {}, { signal });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}
