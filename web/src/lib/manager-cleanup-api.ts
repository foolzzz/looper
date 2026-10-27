import { callV1, V1Error } from "@/lib/v1";

export interface ManagerCleanupRequest {
  manager_id: string;
  retention_days?: number;
}

export interface ManagerCleanupResult {
  manager_id: string;
  deleted_at: string | null;
  retention_days: number;
  eligible: boolean;
  blockers: string[];
  counts: {
    /** Retained ownership tombstones, never rows to delete. */
    threads: number;
    chat_messages: number;
    llm_records: number;
    llm_record_events: number;
    manager_actions: number;
  };
  purged: boolean;
}

async function cleanup(operation: "preview" | "purge", request: ManagerCleanupRequest,
  signal?: AbortSignal): Promise<ManagerCleanupResult> {
  const result = await callV1<ManagerCleanupResult>(`/api/v1/manager_cleanup/${operation}`, request, { signal });
  if ("status" in result) throw new V1Error(202, "needs_confirmation", result.impact);
  return result;
}

export function previewManagerCleanup(request: ManagerCleanupRequest, signal?: AbortSignal) {
  return cleanup("preview", request, signal);
}

/** Call only after the operator confirms the irreversible impact. */
export function purgeManagerCleanup(request: ManagerCleanupRequest, signal?: AbortSignal) {
  return cleanup("purge", request, signal);
}
