import { useEffect, useRef, useState } from "react";
import { fetchTaskRuns, type LlmRecord, type TaskDetail, type TaskRunRow } from "@/lib/api";
import type { TaskRuns } from "@/lib/node-cards";

/** A compact run as the record the cards read: its profile, model and effort, not its prompts or events. */
function asRecord(run: TaskRunRow): LlmRecord {
  const profile = run.profile.name || run.profile.source || run.runtime.name || run.runtime.type;
  return {
    id: run.id,
    agent_id: run.agent_id,
    task_id: null,
    session_id: null,
    status: run.status,
    model: run.model,
    created_at: run.created_at,
    metadata: {
      ...(run.step_id ? { step_id: run.step_id } : {}),
      ...(run.task_role ? { prompt_layers: { task_role: run.task_role } } : {}),
      ...(run.model || run.effort ? { model_choice: { model: run.model ?? "", source: "default", effort: run.effort } } : {}),
      ...(profile
        ? {
            profile: {
              name: run.profile.name ?? "",
              source: run.profile.source,
              runtime: { name: run.runtime.name ?? "", type: run.runtime.type ?? "" },
            },
          }
        : {}),
    },
  } as unknown as LlmRecord;
}

/** The task's Planner, Worker and Reviewer runs, read again whenever a step or the Planner changes (a new run or a
 * run's end moves one); null until read. One compact request (`runs.for_task`). */
export function useTaskRuns(task: TaskDetail | null): { runs: TaskRuns | null; error: string } {
  const [runs, setRuns] = useState<TaskRuns | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);
  const taskId = task?.task_id;
  const changes = task
    ? [
        task.status,
        task.planner_id,
        // A new run of the same agent moves neither status, ids nor steps: the cost's run counts do.
        task.cost ? [task.cost.runs, task.cost.running, ...Object.values(task.cost.by_role).map((r) => r.runs)].join(",") : "",
        ...(task.steps || []).map((s) => [s.id, s.status, s.worker_id, s.reviewer_id, s.review_stage, s.retries].join(":")),
      ].join("|")
    : "";

  useEffect(() => {
    if (!taskId) return;
    const seq = ++latest.current;
    const abort = new AbortController();
    fetchTaskRuns(taskId, abort.signal).then(
      (rows) => {
        if (seq !== latest.current) return;
        const by = (role: TaskRunRow["role"]) => rows.filter((r) => r.role === role).map(asRecord);
        setRuns({ planner: by("planner"), worker: by("worker"), reviewer: by("reviewer") });
        setError("");
      },
      (err) => {
        if (seq === latest.current && !abort.signal.aborted) setError(err instanceof Error ? err.message : String(err));
      },
    );
    return () => abort.abort();
  }, [taskId, changes]);

  return { runs, error };
}
