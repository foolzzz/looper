import { callV1, confirmV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

export interface FenceSettings { enabled: boolean }
export interface UnsupportedManager { manager_id: string; name: string; runtime: string }
export interface OutboundReviewSettings extends FenceSettings { managers_without_hook: UnsupportedManager[] }
export interface ReviewRow {
  id: string;
  created_at: string;
  agent_id: string;
  task_id: string | null;
  parent_record_id: string | null;
  record_id: string | null;
  source: "tool" | "hook" | "post";
  tool: string;
  action: string | null;
  level: "inside" | "operator" | "origin" | "machine" | "external" | "unknown";
  target_kind: string;
  target: string;
  summary: string;
  verdict: "allow" | "block";
  failure: string | null;
  reason: string;
  signals: string[];
  profile_id: string | null;
  model: string | null;
  cost_usd: number | null;
  latency_ms: number | null;
}
export interface ReviewFilters { verdict?: "allow" | "block"; task_id?: string; cursor?: string; limit?: number }
export interface ReviewPage { items: ReviewRow[]; next_cursor: string | null }

async function read<T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const result = await callV1<T>(path, body, { signal });
  if (result && typeof result === "object" && "status" in result && result.status === "needs_confirmation")
    throw new V1Error(202, "unavailable", "The server asked to confirm a read.");
  return result as T;
}

export const fetchOutboundReviewSettings = (signal?: AbortSignal) => read<OutboundReviewSettings>("/api/v1/outbound_review/get", {}, signal);
export const fetchFenceSettings = (signal?: AbortSignal) => read<FenceSettings>("/api/v1/untrusted_fence/get", {}, signal);
export const fetchOutboundReviews = (filters: ReviewFilters = {}, signal?: AbortSignal) => read<ReviewPage>("/api/v1/outbound_review/list", filters, signal);

export function updateOutboundReviewSettings(enabled: boolean, confirmation?: V1PendingConfirmation) {
  return confirmation ? confirmV1<OutboundReviewSettings>(confirmation.confirmation_id, confirmation.session)
    : callV1<OutboundReviewSettings>("/api/v1/outbound_review/update", { enabled });
}
export function updateFenceSettings(enabled: boolean, confirmation?: V1PendingConfirmation) {
  return confirmation ? confirmV1<FenceSettings>(confirmation.confirmation_id, confirmation.session)
    : callV1<FenceSettings>("/api/v1/untrusted_fence/update", { enabled });
}
