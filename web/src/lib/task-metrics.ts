import { callV1, V1Error } from "@/lib/v1";

/** A figure that could not be computed: the others of its answer still are. */
export interface FigureError {
  error: string;
}

/** A share of cases (task-metrics RFC, D11): with fewer than 20 cases only the sample size is given. */
export type Ratio =
  | { insufficient: true; n: number }
  | { insufficient?: undefined; n: number; k: number; value: number; wilson?: [number, number] };

export interface Spread {
  n: number;
  p50_ms: number | null;
  p95_ms: number | null;
}

/** The mean of what a case cost; the cases whose cost is unknown are counted apart, never as 0. */
export interface MeanCost {
  mean_usd: number | null;
  n_known: number;
  unknown: number;
}

/** A figure and the same figure of the comparison window. */
export type Compared<T> = (T & { previous?: T | FigureError }) | FigureError;

export interface StepCost {
  n: number;
  provider_priced: { first: MeanCost; retry: MeanCost; total: MeanCost };
  equivalent: { first: MeanCost; retry: MeanCost; total: MeanCost };
}

export interface TaskMetrics {
  days: number;
  window: { since: string; until: string };
  comparison_window: { since: string; until: string };
  filters: { project: string | null; profile: string | null; difficulty: string | null };
  /** When the records begin: earlier tasks have none (nothing is backfilled). */
  coverage_since: string | null;
  /** Worker runs since then that have no attempt; more than 0 means a write path is broken. */
  missing_attempts: number | null;
  tasks: {
    success_rate: Compared<Ratio>;
    budget_failure_rate: Compared<Ratio>;
    restarts: Compared<{ count: number }>;
    cost_per_completed_task: Compared<{ n: number; provider_priced: MeanCost; equivalent: MeanCost }>;
    duration: Compared<Spread>;
    agent_duration: Compared<Spread>;
    queue_time: Compared<Spread>;
    user_corrections: Compared<{ supplement: Ratio; revision: Ratio; feedback: Ratio }>;
  };
  steps: {
    first_pass_rate: Compared<Ratio>;
    replans: Compared<{ decisions: number; steps_added: number }>;
    attempts_per_accepted_step: Compared<{ n: number; mean: number | null; distribution: Record<string, number> }>;
    rework_rate: Compared<Ratio>;
    failure_kinds: Compared<{ n: number; counts: Record<string, number> }>;
    cost_per_accepted_step: Compared<StepCost>;
    duration: Compared<Spread>;
  };
  reviews: {
    block_rate: Compared<Ratio>;
    effective_blocks: Compared<{ count: number; blocked: number; rate: Ratio }>;
    misses: Compared<{ count: number }>;
    catch_rate: Compared<Ratio>;
    defective_deliveries: Compared<{ count: number; completed: number; by_verdict: Record<string, number> }>;
    adjudication_rate: Compared<Ratio>;
  };
  contract: {
    rates: Compared<{ worker: Ratio; reviewer: Ratio; planner: Ratio }>;
    run_error_rate: Compared<Ratio>;
  };
}

export interface TaskMetricsFilters {
  project?: string;
  profile?: string;
}

export async function fetchTaskMetrics(
  days: number,
  filters: TaskMetricsFilters = {},
  signal?: AbortSignal,
): Promise<TaskMetrics> {
  const body = { days, ...(filters.project ? { project: filters.project } : {}), ...(filters.profile ? { profile: filters.profile } : {}) };
  const result = await callV1<TaskMetrics>("/api/v1/metrics/task_outcomes", body, { signal });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** What runs cost per billing basis: `usd` is null when every run of it has an unknown cost, never 0. */
export interface BasisCost {
  usd: number | null;
  runs: number;
  unknown_runs: number;
}
export interface Cost {
  provider_priced: BasisCost;
  equivalent: BasisCost;
}
export interface CostGroup {
  worker: Cost;
  review: Cost;
  advisor: Cost;
  total: Cost;
}

export interface AttemptView {
  id: number;
  attempt: number;
  task_run_no: number;
  trigger: string;
  profile_id: string | null;
  worker_runs: number;
  run_outcome: string | null;
  review_outcome: string | null;
  review_runs: number;
  blocking_findings: number;
  adjudicated: boolean;
  decision: string | null;
  failure_kind: string | null;
  started_at: string;
  decided_at: string | null;
  cost: CostGroup;
}

export interface StepAttempts {
  task_id: string;
  status: string;
  run_no: number;
  coverage_since: string | null;
  missing_attempts: number;
  steps: { step_key: string; step_id: string | null; task_run_no: number; difficulty: string | null; accepted: boolean; attempts: AttemptView[] }[];
  summary: {
    steps: number;
    attempts: number;
    accepted_steps: number;
    first_pass_steps: number;
    open_attempts: number;
    failure_kinds: Record<string, number>;
  };
  costs: { first: CostGroup; retry: CostGroup; planning: Cost; unattributed: CostGroup; total: Cost };
}

export async function fetchStepAttempts(taskId: string, signal?: AbortSignal): Promise<StepAttempts> {
  const result = await callV1<StepAttempts>("/api/v1/metrics/step_attempts", { task_id: taskId }, { signal });
  if ("confirmation_id" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

export const VERDICTS = ["defect", "incomplete", "wrong_approach", "other"] as const;
export type Verdict = (typeof VERDICTS)[number];

export interface TaskFeedback {
  task_id: string;
  verdict: Verdict | null;
  note: string | null;
  created_at: string | null;
  updated_at: string | null;
}
export const FEEDBACK_NOTE_MAX = 500;

export async function fetchTaskFeedback(taskId: string, signal?: AbortSignal): Promise<TaskFeedback> {
  const result = await callV1<TaskFeedback>("/api/v1/task_feedback/get", { task_id: taskId }, { signal });
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}

/** A null verdict withdraws the mark. */
export async function setTaskFeedback(taskId: string, verdict: Verdict | null, note?: string): Promise<TaskFeedback> {
  const body = { task_id: taskId, verdict, ...(verdict && note ? { note } : {}) };
  const result = await callV1<TaskFeedback>("/api/v1/task_feedback/set", body);
  if ("status" in result) throw new V1Error(202, "unavailable", result.impact);
  return result;
}
