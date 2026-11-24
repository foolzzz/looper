import { callV1, confirmV1, V1Error, type V1PendingConfirmation } from "@/lib/v1";

/** Background roles that have daily caps. Planner, Worker and Manager are not in this list. */
export const COST_GUARD_ROLES = ["processor", "leads_check", "advice_check", "outbound_check", "advisor"] as const;

export type CostGuardMode = "off" | "observe" | "enforce";
export type CostGuardRole = (typeof COST_GUARD_ROLES)[number];

export type CostGuardCaps = { daily_usd: number | null; daily_equivalent_usd: number | null };

export type CostGuardCounter = {
  scope: string;
  scope_ref: string;
  currency: string;
  spent_usd: number;
  reserved_usd: number;
  limit_usd: number | null;
  cost_status: "known" | "unknown";
  unknown_models: string[];
};

/** One admission observation. Distinct from a pending CostHold and from a usage-window hold. */
export type CostGuardObservation = {
  at?: string;
  run_kind: string;
  currency: string;
  estimate_usd: number;
  estimate_source: string;
  decision?: string;
  scope: string | null;
  scope_ref: string | null;
  action: string | null;
  would_hold_until?: string | null;
  would_hold_reason?: string | null;
  note?: string | null;
};

/** A provider usage-window pause returned on the guard view. Not a pending spend decision. */
export type CostGuardUsageHold = {
  provider: string;
  reason: string;
  until: string;
  window: string | null;
  reset_known: boolean;
};

export type CostGuardUnanswered = {
  enabled: boolean;
  window_days: number;
  min_pushes: number;
  pause_after_days: number;
  snooze_days: number;
};

/** Mirrors `looper/agents/v1_domains/cost_guard.py` VIEW. `holds` and `unanswered_pushes` are optional. */
export type CostGuardView = {
  mode: CostGuardMode;
  daily_usd: number | null;
  daily_equivalent_usd: number | null;
  roles: Partial<Record<CostGuardRole, CostGuardCaps>>;
  counters: CostGuardCounter[];
  recent: CostGuardObservation[];
  recommended_daily_usd: number | null;
  recommended_daily_equivalent_usd: number | null;
  problem: string | null;
  holds?: CostGuardUsageHold[];
  unanswered_pushes?: CostGuardUnanswered;
};

/** Sparse update. Omitted leaves stay; `null` clears a cap; `0` is a real cap. */
export type CostGuardPatch = {
  mode?: CostGuardMode | null;
  daily_usd?: number | null;
  daily_equivalent_usd?: number | null;
  roles?: Partial<Record<CostGuardRole, Partial<CostGuardCaps>>>;
  unanswered_pushes?: Partial<CostGuardUnanswered>;
};

/** A pending spend decision (`cost_holds` HOLD). `decided_at` and `decided_via` stay on the type. */
export type CostHold = {
  id: number;
  scope: string;
  scope_ref: string;
  currency: string | null;
  limit_usd: number | null;
  spent_usd: number | null;
  reason: string;
  state: string;
  created_at: string;
  decided_at: string | null;
  decided_via: string | null;
  task_ids?: string[];
  pushes?: number | null;
};

function unwrap<T>(answer: T | V1PendingConfirmation, fallback: string): T {
  if (answer && typeof answer === "object" && "status" in answer) throw new V1Error(202, "unavailable", fallback);
  return answer;
}

export async function fetchCostGuard(signal?: AbortSignal): Promise<CostGuardView> {
  return unwrap(await callV1<CostGuardView>("/api/v1/cost_guard/get", {}, { signal }), "无法读取花费设置。");
}

export async function fetchCostHolds(signal?: AbortSignal): Promise<{ holds: CostHold[] }> {
  return unwrap(await callV1<{ holds: CostHold[] }>("/api/v1/cost_holds/list", {}, { signal }), "无法读取等待决定的花费。");
}

/** Posts the patch, or confirms a previous 202 with the same session. A 202 is not a saved view. */
export function updateCostGuard(patch: CostGuardPatch, confirmation?: V1PendingConfirmation) {
  return confirmation
    ? confirmV1<CostGuardView>(confirmation.confirmation_id, confirmation.session)
    : callV1<CostGuardView>("/api/v1/cost_guard/update", patch);
}
