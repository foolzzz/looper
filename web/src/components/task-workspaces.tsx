import { useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, GitBranch, Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ClampedText } from "@/components/clamped-text";
import {
  type StepWorkspace,
  type TaskDetail,
  type UndoCommand,
  discardStepWorkspace,
  isActiveTask,
} from "@/lib/api";
import { MERGE_ERRORS, shortCommit, workspaceState, workspaceTone } from "@/lib/workspaces";
import { cn } from "@/lib/utils";

const TONE_TEXT = { done: "text-success", problem: "text-destructive", plain: "text-muted-foreground" } as const;

/** A step's worktree marker in the Overview's Steps list: its latest worktree's state. */
export function WorktreeMarker({ workspaces }: { workspaces: StepWorkspace[] | undefined }) {
  const latest = workspaces?.[workspaces.length - 1];
  if (!latest) return null;
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs", TONE_TEXT[workspaceTone(latest)])}>
      <GitBranch className="h-3 w-3" aria-hidden="true" />
      Worktree: {workspaceState(latest)}
    </span>
  );
}

/**
 * The Overview's Step worktrees section, for a task whose steps had worktrees of their own: where its branch started,
 * each worktree oldest first (its state, why it could not be merged and the files in the way, where it still is, and
 * Discard while the task is not running), and the commands that undo the task's commits.
 */
export function TaskWorkspaces({ task, onChanged }: { task: TaskDetail; onChanged: () => void }) {
  const view = task.step_workspaces;
  if (!view || view.workspaces.length === 0) return null;
  const running = isActiveTask(task);
  const branch = view.start?.branch || view.workspaces[0].target_branch;
  return (
    <section aria-labelledby="step-worktrees-title">
      <h3 id="step-worktrees-title" className="mb-1 text-sm font-semibold">Step worktrees</h3>
      <p className="mb-2 text-xs text-muted-foreground">
        Steps that ran alongside others worked on branches of their own; each accepted one was merged into{" "}
        <code className="font-mono">{branch}</code> with a merge commit.
        {view.start && (
          <>
            {" "}The task started with {branch} at <code className="font-mono">{shortCommit(view.start.commit)}</code>.
          </>
        )}
      </p>
      <ul className="divide-y divide-border rounded-lg border border-border bg-card">
        {view.workspaces.map((w) => (
          <WorkspaceRow key={w.id} taskId={task.task_id} workspace={w} running={running} onChanged={onChanged} />
        ))}
      </ul>
      {view.undo.length > 0 && <UndoCommands branch={branch} workDir={task.work_dir} commands={view.undo} />}
    </section>
  );
}

function WorkspaceRow({
  taskId,
  workspace: w,
  running,
  onChanged,
}: {
  taskId: string;
  workspace: StepWorkspace;
  running: boolean;
  onChanged: () => void;
}) {
  const tone = workspaceTone(w);
  const Icon = tone === "done" ? CheckCircle2 : tone === "problem" ? AlertCircle : GitBranch;
  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Icon className={cn("h-4 w-4 shrink-0", TONE_TEXT[tone])} aria-hidden="true" />
        <span className="font-mono text-xs">{w.step_key || w.step_id}</span>
        <span className={cn("text-xs", tone === "plain" ? "text-foreground" : TONE_TEXT[tone])}>{workspaceState(w)}</span>
        <span className="break-all font-mono text-xs text-muted-foreground">{w.branch}</span>
      </div>
      {w.merge_error && (
        <div className="mt-1.5 space-y-1 pl-6">
          <p className="text-xs">{MERGE_ERRORS[w.merge_error]}.</p>
          {w.merge_files.length > 0 ? (
            <ul aria-label="Files in the way of the merge" className="flex flex-wrap gap-1">
              {w.merge_files.map((f) => (
                <li key={f} className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">{f}</li>
              ))}
            </ul>
          ) : (
            w.detail && <ClampedText text={w.detail} className="text-xs text-muted-foreground" />
          )}
        </div>
      )}
      {!w.removed_at && (
        <p className="mt-1.5 break-all pl-6 text-xs text-muted-foreground">
          In <code className="font-mono">{w.path}</code>
          {w.links.length > 0 && <>, linking {w.links.join(", ")}</>}
        </p>
      )}
      {w.discardable && !running && (
        <div className="mt-2 pl-6">
          <DiscardWorkspace taskId={taskId} workspace={w} onDiscarded={onChanged} />
        </div>
      )}
    </li>
  );
}

function DiscardWorkspace({
  taskId,
  workspace,
  onDiscarded,
}: {
  taskId: string;
  workspace: StepWorkspace;
  onDiscarded: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const name = workspace.step_key || workspace.step_id;
  // Focus follows the question: to Cancel when it is asked, back to Discard when it is cancelled.
  const ask = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const asked = useRef(false);
  useEffect(() => {
    if (confirming) cancel.current?.focus();
    else if (asked.current) ask.current?.focus();
    asked.current = confirming;
  }, [confirming]);

  const discard = async () => {
    setBusy(true);
    setError("");
    try {
      await discardStepWorkspace(taskId, workspace.id);
      onDiscarded();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  if (!confirming)
    return (
      <Button
        ref={ask}
        type="button"
        variant="outline"
        size="sm"
        onClick={() => setConfirming(true)}
        aria-label={`Discard the worktree of step ${name}`}
        className="h-11 text-destructive hover:bg-destructive/10 hover:text-destructive sm:h-8"
      >
        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
        Discard
      </Button>
    );
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-2">
      <span className="min-w-0 break-words text-xs text-muted-foreground">
        Discard {name}&apos;s worktree and branch {workspace.branch}? The commits and changes in it that were not merged
        are lost.
      </span>
      <span className="flex items-center gap-2">
        <Button type="button" variant="destructive" size="sm" onClick={discard} disabled={busy} className="h-11 sm:h-8">
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          Discard
        </Button>
        <Button
          ref={cancel}
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => {
            setConfirming(false);
            setError("");
          }}
          disabled={busy}
          className="h-11 sm:h-8"
        >
          Cancel
        </Button>
      </span>
      {error && (
        <span role="alert" className="basis-full text-xs text-destructive">
          Could not discard it: {error}
        </span>
      )}
    </span>
  );
}

function UndoCommands({ branch, workDir, commands }: { branch: string; workDir: string; commands: UndoCommand[] }) {
  return (
    <details className="mt-3 rounded-lg border border-border bg-card">
      <summary className="flex min-h-11 cursor-pointer items-center px-4 text-sm font-medium">
        Undo what the task did to {branch}
      </summary>
      <div className="space-y-3 border-t border-border px-4 py-3">
        <p className="text-xs text-muted-foreground">
          Run these in the task directory, <code className="break-all font-mono">{workDir}</code>. Each one that
          changes {branch} switches to it first, whatever the directory has checked out. Going back to where the task
          started also undoes any commit made on {branch} after it, yours included.
        </p>
        {commands.map((c) => (
          <div key={c.command}>
            <p className="text-xs text-muted-foreground">{c.what}</p>
            <pre className="mt-1 overflow-x-auto rounded bg-muted px-2 py-1.5 font-mono text-xs">
              <code>{c.command}</code>
            </pre>
          </div>
        ))}
      </div>
    </details>
  );
}

/**
 * The Overview's Workspace card: the directory the task works on and how it runs there; for a worktree task its
 * branch, where it started and where its worktree is, or, once done, that the work waits on the branch for the user,
 * with the commands that take it over (run in the workspace). Looper never merges the branch.
 */
export function TaskWorkspaceCard({ task }: { task: TaskDetail }) {
  const view = task.task_workspace;
  if (!view) return null;
  const tree = view.worktree;
  const started = !["init", "ready"].includes(task.status);
  let runs: string;
  if (tree) {
    runs = `In a git worktree, on branch ${tree.branch}, from ${view.base || "what was checked out"} at ${tree.base_commit.slice(0, 12)}`;
  } else if (view.mode === "worktree") {
    runs = started
      ? "In the directory itself: it is not in a git repository with a commit"
      : `In a git worktree, on a branch of its own, made when the task starts${view.base ? ` from ${view.base}` : ""}`;
  } else {
    runs = "In the directory itself";
  }
  return (
    <section aria-labelledby="task-workspace-title" className="rounded-lg border border-border bg-card p-4">
      <h3 id="task-workspace-title" className="text-sm font-semibold">Workspace</h3>
      <dl className="mt-2 grid gap-x-4 gap-y-1 text-xs sm:grid-cols-[max-content_1fr]">
        <dt className="text-muted-foreground">Directory</dt>
        <dd className="break-all font-mono">{view.workspace_dir}</dd>
        <dt className="text-muted-foreground">Runs</dt>
        <dd className="break-words">{runs}</dd>
        {tree?.state === "active" && (
          <>
            <dt className="text-muted-foreground">Worktree</dt>
            <dd className="break-all font-mono">{tree.path}</dd>
          </>
        )}
        {tree && tree.links.length > 0 && (
          <>
            <dt className="text-muted-foreground">Linked</dt>
            <dd className="font-mono">{tree.links.join(", ")}</dd>
          </>
        )}
      </dl>
      {tree?.state === "removed" && (
        <p className="mt-2 flex items-start gap-1.5 text-xs text-success">
          <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span className="break-words">
            The work is on branch <code className="font-mono">{tree.branch}</code>
            {tree.result_commit && <> at <code className="font-mono">{tree.result_commit.slice(0, 12)}</code></>} in{" "}
            <code className="break-all font-mono">{tree.repository}</code>. Merge it when you are ready: Looper does
            not.
          </span>
        </p>
      )}
      {tree && (
        <details className="mt-2">
          <summary className="flex min-h-11 cursor-pointer items-center text-xs font-medium sm:min-h-0">
            Take the work over
          </summary>
          <div className="mt-2 space-y-2">
            <p className="text-xs text-muted-foreground">
              Run these in <code className="break-all font-mono">{view.workspace_dir}</code>.
            </p>
            {tree.commands.map((c) => (
              <div key={c.command}>
                <p className="text-xs text-muted-foreground">{c.what}</p>
                <pre className="mt-1 overflow-x-auto rounded bg-muted px-2 py-1.5 font-mono text-xs">
                  <code>{c.command}</code>
                </pre>
              </div>
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
