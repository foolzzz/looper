import { callV1, V1Error } from "@/lib/v1";

/** Operator-only collection facts; ids stay decimal strings across the JSON boundary. */
export interface SourceStreamAlert {
  id: string;
  kind: string;
  opened_at: string;
  resolved_at: string | null;
  parity_session_id: string | null;
  metadata: Record<string, unknown>;
  open_state: string;
  recovery_state: string;
}

export interface CollectionParity {
  session: string;
  state: "pending" | "matched" | "drift" | "incomplete";
  reason: string | null;
  empty_window: boolean;
  window: { from: string; to: string };
  coverage: { complete: number; pending: number; failed: number };
  totals: { bridge: number; collect: number; bridge_only: number; collect_only: number; identity_conflicts?: number };
  buckets?: { chat_id: string; hour: string; bridge_only: number; collect_only: number; identity_conflicts: number }[];
}

export interface MessageCollection {
  source_id: string;
  ingest_mode: "off" | "bridge" | "collect";
  generation: string;
  settings: { collect_cron: string; alerts_enabled: boolean };
  auth: { state: string | null; checked_at: string | null; refresh_expires_at: string | null };
  last_collect_ok_at: string | null;
  collect_enabled_at: string | null;
  debt: { chats: number; threads: number };
  fallback_24h?: { events_new: number; sweep_found: number; ratio: number | null };
  latest_run: { run_id: string; status: string; search_hits: number | null; unindexed: number | null;
    sweep_chats: number | null; sweep_found: number | null; events_new: number; events_seen: number;
    chats_failed: number | null; threads_failed: number | null } | null;
  parity: CollectionParity | null;
  alerts: SourceStreamAlert[];
}

export async function getMessageCollection(sourceId: string, signal?: AbortSignal): Promise<MessageCollection> {
  const result = await callV1<MessageCollection>("/api/v1/message_collection/get", { source_id: sourceId }, { signal });
  if ("status" in result && result.status === "needs_confirmation")
    throw new V1Error(202, "unavailable", "读取采集状态需要确认。");
  return result as MessageCollection;
}
