import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TaskWorkspaceCard, TaskWorkspaces, WorktreeMarker } from "@/components/task-workspaces";
import { WorkspaceSummary } from "@/components/task-workspace-field";
import { executionChanges, executionDraft } from "@/components/project-execution";
import type { Project, StepWorkspace, TaskDetail, WorkspaceCheck } from "@/lib/api";

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
  const project = { workspace_links: [".venv", ".env"] } as Project;

  it("are one path per line, and blank lines are dropped", () => {
    const draft = executionDraft(project);
    expect(draft.workspace_links).toBe(".venv\n.env");
    expect(executionChanges(draft, project)).toEqual({});
    expect(executionChanges({ ...draft, workspace_links: ".venv\n\n  node_modules \n" }, project))
      .toEqual({ workspace_links: [".venv", "node_modules"] });
    expect(executionChanges({ ...draft, workspace_links: " \n" }, project)).toEqual({ workspace_links: null });
  });
});

describe("TaskWorkspaceCard", () => {
  const tree = {
    branch: "looper/t1/task", base_commit: "a".repeat(40), path: "/rt/task-worktrees/t1", repository: "/srv/calc",
    links: [".env"], state: "active" as const, result_commit: null, removed_at: null,
    commands: [{ what: "Merge it into the branch you have checked out", command: "git merge looper/t1/task" }],
  };
  const of = (status: string, view: TaskDetail["task_workspace"]) =>
    text(renderToStaticMarkup(<TaskWorkspaceCard task={{ task_id: "t1", status, task_workspace: view } as TaskDetail} />));

  it("says where a worktree task runs, then where its work waits", () => {
    const running = of("running", { workspace_dir: "/srv/calc", mode: "worktree", base: "", worktree: tree });
    for (const part of ["Directory/srv/calc", "RunsIn a git worktree, on branch looper/t1/task, from what was checked out at aaaaaaaaaaaa",
                        "Worktree/rt/task-worktrees/t1", "Linked.env", "Take the work over", "git merge looper/t1/task"])
      expect(running).toContain(part);
    const done = of("completed", { workspace_dir: "/srv/calc", mode: "worktree", base: "main",
                                   worktree: { ...tree, state: "removed", result_commit: "b".repeat(40) } });
    expect(done).toContain("The work is on branch looper/t1/task at bbbbbbbbbbbb in /srv/calc. Merge it when you are ready: Looper does not.");
    expect(done).not.toContain("Worktree/rt");
  });

  it("says when the worktree is still to come, or why the task ran in place", () => {
    expect(of("init", { workspace_dir: "/srv/calc", mode: "worktree", base: "v1", worktree: null }))
      .toContain("In a git worktree, on a branch of its own, made when the task starts from v1");
    expect(of("running", { workspace_dir: "/tmp/x", mode: "worktree", base: "", worktree: null }))
      .toContain("In the directory itself: it is not in a git repository with a commit");
    expect(of("running", { workspace_dir: "/srv/calc", mode: "in_place", base: "", worktree: null }))
      .toContain("RunsIn the directory itself");
    expect(renderToStaticMarkup(<TaskWorkspaceCard task={{ task_id: "t1", status: "running" } as TaskDetail} />)).toBe("");
  });
});

describe("WorkspaceSummary", () => {
  const check: WorkspaceCheck = {
    workspace: "/srv/calc", mode: "worktree", requested_mode: "worktree", default_mode: "worktree",
    git: { repository: "/srv/calc", branch: "main", head: "c".repeat(40), base_commit: "c".repeat(40) },
    warnings: [{ kind: "uncommitted", text: "2 uncommitted change(s) in the workspace will not be in the task's worktree" }],
    notes: { found: true, files: ["MEMORY.md", "deploy.md"], omitted: [] }, notes_on: false,
  };

  it("names the repository, every warning and whether the notes reach the agents", () => {
    const shown = text(renderToStaticMarkup(<WorkspaceSummary check={check} />));
    expect(shown).toContain("A worktree of /srv/calc, from main at cccccccccccc; the work stays on the task's branch until you merge it.");
    expect(shown).toContain("2 uncommitted change(s)");
    expect(shown).toContain("Your Claude Code notes for this directory (2 files): the project does not give them to its agents");
    expect(text(renderToStaticMarkup(<WorkspaceSummary check={{ ...check, notes_on: true }} />)))
      .toContain("its agents get a read-only copy.");
    expect(text(renderToStaticMarkup(<WorkspaceSummary check={{ ...check, mode: "in_place", git: null, warnings: [],
                                                               notes: { found: false, files: [], omitted: [] } }} />)))
      .toBe("In /srv/calc itself (not a git repository).");
  });
});
