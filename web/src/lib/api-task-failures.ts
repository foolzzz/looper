import { callV1, confirmV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

export type FailureClass = "quota" | "transient" | "environment" | "judgment";
export type FailureMode = "off" | "record" | "act";
export interface StepFailure {
  id: number; task_id: string; step_id: string | null; step_identity: string; step_key: string;
  record_id: number | null; role: "worker" | "reviewer"; failure_class: FailureClass; failure_cause: string;
  streak: number; action: "hold" | "planner" | "breaker"; created_at: string; run_id: string;
  attempt: string | null; epoch: number; target: Record<string, unknown>; result: Record<string, unknown>;
}
export interface FailureHold {
  step_id: string; task_id: string; kind: Exclude<FailureClass, "judgment"> | "breaker"; cause: string;
  provider_id: string | null; until: string | null; failure_id: number; created_at: string; streak: number;
  target: Record<string, unknown>; targets: Record<string, unknown>[]; result: Record<string, unknown>;
  run_id: string; role: string; step_key: string; reminded_at: string | null;
}
export interface FailureHistory { items: StepFailure[]; next_after_id: number | null }
export interface HoldRelease { task_id: string; step_id: string; failure_id: number; action: "retry" | "planner" | "stop" }
export interface EngineSettings { failure_typing: { value: FailureMode; source: "setting" | "built-in"; built_in: "off" } }
export function isConfirmation(value: unknown): value is V1PendingConfirmation {
  return typeof value === "object" && value !== null && "confirmation_id" in value;
}
async function read<T>(path: string, body: unknown): Promise<T> {
  const value = await callV1<T>(`/api/v1/${path}`, body);
  if (isConfirmation(value)) throw new V1Error(202, "unavailable", "读取操作意外要求确认。");
  return value;
}
export function fetchFailureHistory(taskId: string, options: { step_id?: string; after_id?: number; limit?: number } = {}) {
  return read<FailureHistory>("tasks/failure_history", { task_id: taskId, ...options });
}
export function fetchFailureHolds(taskId: string) { return read<{ holds: FailureHold[] }>("tasks/holds", { task_id: taskId }); }
export function releaseFailureHold(action: HoldRelease, confirmation?: V1PendingConfirmation) {
  return confirmation ? confirmV1<HoldRelease & { status: string }>(confirmation.confirmation_id, confirmation.session)
    : callV1<HoldRelease & { status: string }>("/api/v1/tasks/hold_release", action);
}
export function fetchEngineSettings() { return read<EngineSettings>("engine_settings/get", {}); }
export function updateEngineSettings(mode: FailureMode | null, confirmation?: V1PendingConfirmation) {
  return confirmation ? confirmV1<EngineSettings>(confirmation.confirmation_id, confirmation.session)
    : callV1<EngineSettings>("/api/v1/engine_settings/update", { failure_typing: mode });
}
