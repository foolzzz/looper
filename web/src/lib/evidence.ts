/** Acceptance evidence for a Pipeline step card: the `task_evidence.get` read, and which run is current.

Current evidence is the greatest `run` of the latest `attempt` for each check. Older attempts keep
their own greatest run. The server returns every run; this module chooses. */
import { useEffect, useState } from "react";
import type { AcceptanceCheck, TaskStep } from "@/lib/api";
import { callV1 } from "@/lib/v1";

const ERROR_WORDS: Record<string, string> = {
  could_not_start: "could not start",
  timeout: "timed out",
  changed_code: "changed the code",
  output_limit: "output too large",
  artifact_invalid: "artifact invalid",
  evidence_unavailable: "evidence unavailable",
  interrupted: "interrupted",
  budget: "out of budget",
  sandbox_unavailable: "sandbox unavailable",
};

const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

export interface EvidenceArtifact {
  name: string;
  mime: string;
  bytes: number;
  sha256: string;
}

export interface EvidenceRun {
  id: number;
  task_id: string;
  step_id: string;
  check_id: string;
  attempt: number;
  run: number;
  state: string;
  error_kind: string | null;
  exit_code: number | null;
  duration_ms: number | null;
  head_sha: string | null;
  guarded: boolean;
  required: boolean;
  output_tail: string;
  artifacts: EvidenceArtifact[];
  protected_changed: string[];
}

export interface EvidenceStep {
  step_id: string;
  step: string;
  checks: AcceptanceCheck[] | null;
  runs: EvidenceRun[];
}

export interface TaskEvidence {
  task_id: string;
  steps: EvidenceStep[];
}

export type EvidenceLoad =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; error: string }
  | { status: "ready"; data: TaskEvidence };

export interface EvidenceArtifactView {
  name: string;
  mime: string;
  href: string;
  image: boolean;
}

export interface EvidenceRunView {
  attempt: number;
  run: number;
  state: string;
  words: string;
  exitCode: number | null;
  durationMs: number | null;
  head: string | null;
  /** A run with no private checkout. Absent until a run is recorded. */
  unguarded: boolean;
  /** A check that does not block the step. Absent until a run is recorded. */
  advisory: boolean;
  outputTail: string;
  artifacts: EvidenceArtifactView[];
  protectedChanged: string[];
}

export interface EvidenceCheckView {
  id: string;
  command: string;
  preview: string;
  current: EvidenceRunView | null;
  earlier: EvidenceRunView[];
}

export interface EvidenceCriterionView {
  text: string;
  checks: EvidenceCheckView[];
  noCheck: boolean;
}

export interface EvidenceView {
  status: "loading" | "error" | "ready";
  error?: string;
  criteria: EvidenceCriterionView[];
}

export function stateWords(state: string, errorKind: string | null = null): string {
  switch (state) {
    case "passed": return "Passed";
    case "failed": return "Failed";
    case "stale": return "Stale";
    case "running": return "Running";
    case "error": return `Error: ${errorKind ? (ERROR_WORDS[errorKind] ?? errorKind) : "unknown"}`;
    default: return state;
  }
}

/** The command's first 80 characters; the card shows the rest only while the row is expanded. */
export function commandPreview(command: string, limit = 80): string {
  return command.length > limit ? `${command.slice(0, limit)}…` : command;
}

export function stepsNeedEvidence(steps: readonly { checks?: readonly unknown[] | null }[]): boolean {
  return steps.some((step) => (step.checks?.length ?? 0) > 0);
}

export function artifactHref(taskId: string, runId: number, index: number): string {
  return `/api/evidence/${encodeURIComponent(taskId)}/${runId}/${index}`;
}

function runView(run: EvidenceRun, taskId: string): EvidenceRunView {
  return {
    attempt: run.attempt,
    run: run.run,
    state: run.state,
    words: stateWords(run.state, run.error_kind),
    exitCode: run.exit_code,
    durationMs: run.duration_ms,
    head: run.head_sha ? run.head_sha.slice(0, 12) : null,
    unguarded: run.guarded === false,
    advisory: run.required === false,
    outputTail: run.output_tail ?? "",
    artifacts: (run.artifacts ?? []).map((artifact, index) => ({
      name: artifact.name,
      mime: artifact.mime,
      href: artifactHref(taskId, run.id, index),
      image: IMAGE_TYPES.has(artifact.mime),
    })),
    protectedChanged: run.protected_changed ?? [],
  };
}

function checkView(check: AcceptanceCheck, runs: EvidenceRun[], taskId: string): EvidenceCheckView {
  const byAttempt = new Map<number, EvidenceRun>();
  for (const run of runs) {
    if (run.check_id !== check.id) continue;
    const prev = byAttempt.get(run.attempt);
    if (!prev || run.run > prev.run) byAttempt.set(run.attempt, run);
  }
  const attempts = [...byAttempt.keys()].sort((a, b) => b - a);
  const [latest, ...older] = attempts;
  return {
    id: check.id,
    command: check.command,
    preview: commandPreview(check.command),
    current: latest === undefined ? null : runView(byAttempt.get(latest)!, taskId),
    earlier: older.map((attempt) => runView(byAttempt.get(attempt)!, taskId)),
  };
}

/** One row per acceptance criterion. Null when the step has no checks, so the card stays as it was. */
export function buildEvidenceView(
  step: Pick<TaskStep, "id" | "acceptance_criteria" | "checks">,
  load: EvidenceLoad,
  taskId: string,
): EvidenceView | null {
  if (!step.checks?.length) return null;
  if (load.status === "error") return { status: "error", error: load.error || "Could not read evidence.", criteria: [] };
  if (load.status !== "ready") return { status: "loading", criteria: [] };
  const runs = load.data.steps.find((item) => item.step_id === step.id)?.runs ?? [];
  const texts = step.acceptance_criteria ?? [];
  const numbers = new Set<number>(texts.map((_, index) => index + 1));
  for (const check of step.checks) numbers.add(check.criterion);
  const criteria = [...numbers].sort((a, b) => a - b).map((criterion) => {
    const checks = step.checks!.filter((check) => check.criterion === criterion);
    return {
      text: texts[criterion - 1] ?? `Criterion ${criterion}`,
      checks: checks.map((check) => checkView(check, runs, taskId)),
      noCheck: checks.length === 0,
    };
  });
  return { status: "ready", criteria };
}

export async function fetchTaskEvidence(taskId: string, signal?: AbortSignal): Promise<TaskEvidence> {
  const result = await callV1<TaskEvidence>("/api/v1/task_evidence/get", { task: taskId }, { signal });
  if (!result || typeof result !== "object" || !("steps" in result) || !Array.isArray(result.steps)) {
    throw new Error("Could not read evidence.");
  }
  return result;
}

export function useStepEvidence(taskId: string, needed: boolean): EvidenceLoad {
  const [load, setLoad] = useState<EvidenceLoad>({ status: "idle" });
  useEffect(() => {
    if (!needed) {
      setLoad({ status: "idle" });
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setLoad({ status: "loading" });
    fetchTaskEvidence(taskId, controller.signal).then(
      (data) => { if (!cancelled) setLoad({ status: "ready", data }); },
      (error: unknown) => {
        if (cancelled || controller.signal.aborted) return;
        const message = error instanceof Error && error.message ? error.message : "Could not read evidence.";
        setLoad({ status: "error", error: message });
      },
    );
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [taskId, needed]);
  return load;
}
