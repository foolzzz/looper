import { callV1, V1Error } from "@/lib/v1";

/** A latency's percentiles in milliseconds; null when no turn in the window measured it. */
export interface Percentiles {
  p50_ms: number | null;
  p95_ms: number | null;
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
}

export async function fetchManagerMetrics(days: number, signal?: AbortSignal): Promise<ManagerMetrics> {
  const result = await callV1<ManagerMetrics>("/api/v1/manager_metrics/get", { days }, { signal });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}
