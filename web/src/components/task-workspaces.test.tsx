import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TaskWorkspaces, WorktreeMarker } from "@/components/task-workspaces";
import { executionChanges, executionDraft } from "@/components/project-execution";
import type { Project, StepWorkspace, TaskDetail } from "@/lib/api";

function workspace(id: number, step: string, extra: Partial<StepWorkspace> = {}): StepWorkspace {
  return {
    id, step_id: `id-${step}`, step_key: step, branch: `looper/t1/${step}-${id}`, target_branch: "main",
    base_commit: "b".repeat(40), checkpoint: false, result_commit: null, merge_commit: null, state: "active",
    merge_error: null, merge_files: [], detail: "", links: [], path: `/rt/worktrees/t1/${step}-${id}`, removed_at: null,
    discardable: true, ...extra,
  };
}

const MERGED = workspace(1, "s1", { state: "merged", merge_commit: "c".repeat(40), removed_at: "2026-10-17T08:00:00Z",
                                    discardable: false });
const UNMERGED = workspace(2, "s2", { state: "unmerged", merge_error: "conflict", merge_files: ["a.py", "b.py"],
                                      detail: "conflicts with main in a.py, b.py", links: [".venv"] });
const UNDO = [{ what: "Undo step s1", command: `git revert -m 1 ${"c".repeat(40)}` }];

function task(group: TaskDetail["group"], workspaces: StepWorkspace[] = [MERGED, UNMERGED]): TaskDetail {
  return {
    task_id: "t1", group, work_dir: "/srv/calc",
    step_workspaces: { start: { branch: "main", commit: "a".repeat(40) }, workspaces, undo: UNDO },
  } as TaskDetail;
}

const text = (html: string) => html.replace(/<[^>]+>/g, "").replace(/&#x27;/g, "'");

describe("TaskWorkspaces", () => {
  it("shows where the task started, each worktree, why one did not merge and how to undo the task", () => {
    const shown = text(renderToStaticMarkup(<TaskWorkspaces task={task("attention")} onChanged={() => {}} />));
    for (const part of [
      "Step worktrees", "The task started with main at aaaaaaaaaaaa.",
      "s1Merged as cccccccccccclooper/t1/s1-1",
      "s2Not merged: conflictlooper/t1/s2-2", "Its changes conflict with the branch.", "a.pyb.py",
      "In /rt/worktrees/t1/s2-2, linking .venv",
      "Undo what the task did to main", "Run these in the task directory, /srv/calc.",
      "Each one that changes main switches to it first, whatever the directory has checked out.",
      `Undo step s1git revert -m 1 ${"c".repeat(40)}`,
    ]) expect(shown).toContain(part);
    // Only the one still there, of a task that is not running, can be discarded; the merged one is gone.
    expect(shown.match(/Discard/g)).toHaveLength(1);
    expect(shown).not.toContain("In /rt/worktrees/t1/s1-1");
  });

  it("offers no discard while the task runs, and is absent for a task without worktrees", () => {
    expect(text(renderToStaticMarkup(<TaskWorkspaces task={task("in_progress")} onChanged={() => {}} />)))
      .not.toContain("Discard");
    expect(renderToStaticMarkup(<TaskWorkspaces task={task("attention", [])} onChanged={() => {}} />)).toBe("");
    expect(renderToStaticMarkup(<TaskWorkspaces task={{ ...task("attention"), step_workspaces: null }}
                                                onChanged={() => {}} />)).toBe("");
  });

  it("marks a step with its latest worktree's state", () => {
    expect(text(renderToStaticMarkup(<WorktreeMarker workspaces={[UNMERGED, workspace(3, "s2")]} />)))
      .toBe("Worktree: Working in its worktree");
    expect(text(renderToStaticMarkup(<WorktreeMarker workspaces={[{ ...MERGED, merge_commit: null }]} />)))
      .toBe("Worktree: Merged: it changed nothing");
    expect(renderToStaticMarkup(<WorktreeMarker workspaces={undefined} />)).toBe("");
  });
});

describe("the project's links in its editor", () => {
  const project = { step_workspace_links: [".venv", ".env"] } as Project;

  it("are one path per line, and blank lines are dropped", () => {
    const draft = executionDraft(project);
    expect(draft.step_workspace_links).toBe(".venv\n.env");
    expect(executionChanges(draft, project)).toEqual({});
    expect(executionChanges({ ...draft, step_workspace_links: ".venv\n\n  node_modules \n" }, project))
      .toEqual({ step_workspace_links: [".venv", "node_modules"] });
    expect(executionChanges({ ...draft, step_workspace_links: " \n" }, project)).toEqual({ step_workspace_links: null });
  });
});
