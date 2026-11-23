import { callV1, confirmV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

export interface MetricsAlertsSettings {
  enabled: boolean;
  last_checked: string | null;
  rules: { rule: string; state: string; sent_on: string | null; tier: number }[];
}

export async function fetchMetricsAlerts(signal?: AbortSignal): Promise<MetricsAlertsSettings> {
  const answer = await callV1<MetricsAlertsSettings>("/api/v1/metrics_alerts/get", {}, { signal });
  if ("status" in answer) throw new V1Error(202, "unavailable", answer.impact);
  return answer;
}

export function updateMetricsAlerts(enabled: boolean, confirmation?: V1PendingConfirmation) {
  return confirmation
    ? confirmV1<MetricsAlertsSettings>(confirmation.confirmation_id, confirmation.session)
    : callV1<MetricsAlertsSettings>("/api/v1/metrics_alerts/update", { enabled });
}
