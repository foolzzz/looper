import { callV1, V1Error } from "@/lib/v1";

/** A latency's percentiles in milliseconds; null when no turn in the window measured it. */
export interface Percentiles {
  p50_ms: number | null;
  p95_ms: number | null;
}

/** Current queue and breaker state, with delivery outcomes and gateway events over 24 hours. */
export interface OutboundChannelMetrics {
  channel_id: string;
  name: string;
  queued: number;
  backoffs_24h: number;
  breaker_opens_24h: number;
  failed_24h: Record<string, number>;
  uncertain_24h: number;
  breaker_state: "closed" | "open" | "half_open" | null;
}

/** The answer of `manager_metrics.get` (assistant-layer RFC 4.1.3): the Manager's turns and proactive messages over
 * the last `window_days` days. `enabled` is the metrics switch (Settings → Chat). */
export interface ManagerMetrics {
  enabled: boolean;
  window_days: number;
  turns: { by_delivery: Record<string, number> };
  ack: Percentiles & { by_chat: Record<string, Percentiles> };
  first_reply: Percentiles & { by_chat: Record<string, Percentiles> };
  /** Turns that delivered nothing, and their share of the turns; `ratio` is null with no turn. */
  empty: { count: number; ratio: number | null };
  proactive: {
    /** One row per server-local day that had a proactive message, oldest first. */
    per_day: { date: string; by_source: Record<string, number> }[];
    /** Messages answered within 30 minutes; null with no message old enough. */
    reply_rate: { all: number | null; by_source: Record<string, number | null> };
  };
  /** Optional while a dashboard connects to a server predating outbound measurements. */
  outbound?: { enabled: false } | OutboundChannelMetrics[];
  outbound_started_at?: string;
  /** Backoff and breaker counts have a shorter window after a service restart. */
  outbound_since_start?: boolean;
}

export async function fetchManagerMetrics(days: number, signal?: AbortSignal): Promise<ManagerMetrics> {
  const result = await callV1<ManagerMetrics>("/api/v1/manager_metrics/get", { days }, { signal });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}
