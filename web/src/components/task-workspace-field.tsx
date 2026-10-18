import { useEffect, useState } from "react";
import { AlertTriangle, GitBranch, Loader2, NotebookText } from "lucide-react";
import { type WorkspaceCheck, type WorkspaceMode, checkWorkspace } from "@/lib/api";

/** How a new task runs in its directory, as the create form holds it: "" is the default. */
export interface WorkspaceChoice {
  mode: WorkspaceMode | "";
  base: string;
}

export const MODE_LABELS: Record<WorkspaceMode, string> = {
  worktree: "In a git worktree, on a branch of its own",
  in_place: "In the directory itself",
};

const SELECT = "mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/50";

/**
 * The create form's workspace part: how the task runs (the default as the server resolves it, or a choice), where a
 * worktree's branch starts, and what the directory holds, checked as it changes: its repository, the warnings (never
 * a refusal) and whether the operator's notes for it reach the agents.
 */
export function TaskWorkspaceField({
  dir,
  projectId,
  value,
  onChange,
}: {
  /** The directory the form names, else the one the task would get ("" for none yet). */
  dir: string;
  /** null for a project the form is about to create. */
  projectId: string | null;
  value: WorkspaceChoice;
  onChange: (value: WorkspaceChoice) => void;
}) {
  const [check, setCheck] = useState<WorkspaceCheck | null>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    if (!dir && !projectId) {
      setCheck(null);
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setChecking(true);
      checkWorkspace({ dir: dir || undefined, project_id: projectId || undefined, mode: value.mode || undefined,
                       base: value.base.trim() || undefined }, controller.signal)
        .then((found) => {
          setCheck(found);
          setError("");
        })
        .catch((err) => {
          if (controller.signal.aborted) return;
          setCheck(null);
          setError(err instanceof Error ? err.message : String(err));
        })
        .finally(() => !controller.signal.aborted && setChecking(false));
    }, 400);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [dir, projectId, value.mode, value.base]);

  const requested = value.mode || check?.default_mode || "worktree";
  return (
    <div className="space-y-2">
      <div className="grid gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="new-task-workspace-mode" className="text-sm font-medium text-muted-foreground">Runs</label>
          <select
            id="new-task-workspace-mode"
            value={value.mode}
            onChange={(e) => {
              const mode = e.target.value as WorkspaceMode | "";
              onChange({ mode, base: (mode || check?.default_mode) === "in_place" ? "" : value.base });
            }}
            className={SELECT}
          >
            <option value="">Default{check?.default_mode ? `: ${MODE_LABELS[check.default_mode].toLowerCase()}` : ""}</option>
            <option value="worktree">{MODE_LABELS.worktree}</option>
            <option value="in_place">{MODE_LABELS.in_place}</option>
          </select>
        </div>
        {requested === "worktree" && (
          <div>
            <label htmlFor="new-task-workspace-base" className="text-sm font-medium text-muted-foreground">Starts from</label>
            <input
              id="new-task-workspace-base"
              type="text"
              value={value.base}
              onChange={(e) => onChange({ ...value, base: e.target.value })}
              placeholder={check?.git?.branch ? `Default: ${check.git.branch}` : "Default: what is checked out"}
              className={`${SELECT} font-mono`}
            />
          </div>
        )}
      </div>
      <div role="status" aria-live="polite" className="space-y-1.5 text-xs text-muted-foreground">
        {checking && !check && (
          <p className="flex items-center gap-1.5">
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            Checking the directory…
          </p>
        )}
        {error && <p className="text-destructive">{error}</p>}
        {check && !error && <WorkspaceSummary check={check} />}
      </div>
    </div>
  );
}

/** What the check found: where the task runs, each warning, and the operator's notes. */
export function WorkspaceSummary({ check }: { check: WorkspaceCheck }) {
  const git = check.git;
  return (
    <>
      <p className="flex items-start gap-1.5">
        <GitBranch className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span className="break-all">
          {check.mode === "worktree" && git
            ? `A worktree of ${git.repository}, from ${git.branch ?? "a detached HEAD"} at ${(git.base_commit ?? git.head).slice(0, 12)}; the work stays on the task's branch until you merge it.`
            : git
              ? `In ${check.workspace} itself, on ${git.branch ?? "a detached HEAD"}.`
              : `In ${check.workspace} itself (not a git repository).`}
        </span>
      </p>
      {check.warnings.map((warning) => (
        <p key={warning.kind} className="flex items-start gap-1.5 text-warning">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span className="break-words">{warning.text}</span>
        </p>
      ))}
      {check.notes.found && (
        <p className="flex items-start gap-1.5">
          <NotebookText className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span>
            Your Claude Code notes for this directory ({check.notes.files.length} file
            {check.notes.files.length === 1 ? "" : "s"}):{" "}
            {check.notes_on === false
              ? "the project does not give them to its agents (Project settings → Operator's notes)."
              : "its agents get a read-only copy."}
          </span>
        </p>
      )}
    </>
  );
}
