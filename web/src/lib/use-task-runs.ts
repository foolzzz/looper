import { useEffect, useRef, useState } from "react";
import { fetchLlmRecords, type TaskDetail } from "@/lib/api";
import type { TaskRuns } from "@/lib/node-cards";

// The most runs of one role `GET /api/llm-records` answers with.
export const RUN_LIMIT = 500;

/** The task's Planner, Worker and Reviewer runs, read again whenever a step or the Planner changes (a new run or a
 * run's end moves one); null until read. */
export function useTaskRuns(task: TaskDetail | null): { runs: TaskRuns | null; error: string } {
  const [runs, setRuns] = useState<TaskRuns | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);
  const taskId = task?.task_id;
  const changes = task
    ? [
        task.status,
        task.planner_id,
        ...(task.steps || []).map((s) => [s.id, s.status, s.worker_id, s.reviewer_id, s.review_stage, s.retries].join(":")),
      ].join("|")
    : "";

  useEffect(() => {
    if (!taskId) return;
    const seq = ++latest.current;
    Promise.all(
      (["planner", "worker", "reviewer"] as const).map((role) => fetchLlmRecords({ task_id: taskId, role, limit: RUN_LIMIT })),
    ).then(
      ([planner, worker, reviewer]) => {
        if (seq !== latest.current) return;
        setRuns({ planner, worker, reviewer });
        setError("");
      },
      (err) => {
        if (seq === latest.current) setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, [taskId, changes]);

  return { runs, error };
}
