/** A task's step worktrees in words, as the Steps list, the Pipeline's step cards and the Step worktrees section show
 * them. */
import type { MergeError, StepWorkspace, TaskDetail } from "@/lib/api";

/** Why an accepted step's branch could not be merged into the task directory's branch. */
export const MERGE_ERRORS: Record<MergeError, string> = {
  conflict: "Its changes conflict with the branch",
  local_changes: "Uncommitted changes in the task directory were in the way",
  branch_changed: "The task directory left the branch, or the branch lost the step's start",
  step_branch_changed: "The step's checkout left its branch, or its commits were rewritten",
  git_failed: "A git command failed",
};

export const shortCommit = (commit: string | null | undefined) => (commit ?? "").slice(0, 12);

/** Where a workspace stands, in a few words: "working", "merged as abc", "not merged: conflict". */
export function workspaceState(w: StepWorkspace): string {
  switch (w.state) {
    case "active":
      return "Working in its worktree";
    case "merging":
      return "Being merged";
    case "merged":
      return w.merge_commit ? `Merged as ${shortCommit(w.merge_commit)}` : "Merged: it changed nothing";
    case "unmerged":
      return `Not merged: ${w.merge_error ?? "unknown"}`;
    case "discarded":
      return w.removed_at ? "Discarded" : "Discarded, its branch kept";
  }
}

/** The tone of a workspace's state: done, needs a look, or neither. */
export function workspaceTone(w: StepWorkspace): "done" | "problem" | "plain" {
  if (w.state === "merged") return "done";
  if (w.state === "unmerged") return "problem";
  return "plain";
}

/** Each step's worktrees, oldest first, by step id; empty when the task had none. */
export function workspacesByStep(task: Pick<TaskDetail, "step_workspaces">): Map<string, StepWorkspace[]> {
  const byStep = new Map<string, StepWorkspace[]>();
  for (const w of task.step_workspaces?.workspaces ?? []) byStep.set(w.step_id, [...(byStep.get(w.step_id) ?? []), w]);
  return byStep;
}
