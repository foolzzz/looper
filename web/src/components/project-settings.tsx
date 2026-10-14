import { useId, useState } from "react";
import { CheckCircle2, Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { deleteProject, updateProject, type Project, type ProjectUpdate } from "@/lib/api";
import { ProjectBackgroundField, backgroundTooLong } from "@/components/project-background-field";
import { ProjectHarnessOverrides, harnessTooLong } from "@/components/harness-field";
import {
  ProjectExecutionFields,
  executionChanges,
  executionDraft,
  type ExecutionDraft,
} from "@/components/project-execution";

interface Draft {
  name: string;
  workDir: string;
  description: string;
  background: string;
  plannerHarness: string;
  workerHarness: string;
  execution: ExecutionDraft;
}

function draftOf(project: Project): Draft {
  return {
    name: project.name,
    workDir: project.work_dir,
    description: project.description,
    background: project.background,
    plannerHarness: project.planner_harness_prompt ?? "",
    workerHarness: project.worker_harness_prompt ?? "",
    execution: executionDraft(project),
  };
}

/** What the draft changes, as the API takes it; an empty directory, background or harness clears it. */
function changesOf(draft: Draft, project: Project): ProjectUpdate {
  const fields: ProjectUpdate = executionChanges(draft.execution, project);
  if (draft.name.trim() !== project.name) fields.name = draft.name.trim();
  if (draft.description.trim() !== project.description) fields.description = draft.description.trim();
  if (draft.workDir.trim() !== project.work_dir) fields.work_dir = draft.workDir.trim();
  if (draft.background !== project.background) fields.background = draft.background;
  if (draft.plannerHarness !== (project.planner_harness_prompt ?? "")) fields.planner_harness_prompt = draft.plannerHarness;
  if (draft.workerHarness !== (project.worker_harness_prompt ?? "")) fields.worker_harness_prompt = draft.workerHarness;
  return fields;
}

/** A project's fields and execution settings, saved together; and deleting the project once it has no tasks. */
export function ProjectSettingsForm({ project, onDeleted }: { project: Project; onDeleted: () => void }) {
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => draftOf(project));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const set = <K extends keyof Draft>(key: K) => (value: Draft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setSaved(false);
  };
  const changes = changesOf(draft, project);
  const dirty = Object.keys(changes).length > 0;
  const tooLong =
    backgroundTooLong(draft.background) || harnessTooLong(draft.plannerHarness) || harnessTooLong(draft.workerHarness);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft.name.trim()) return setError("A project name is required.");
    if (!dirty || tooLong) return;
    setBusy(true);
    setError("");
    try {
      setDraft(draftOf(await updateProject(project.project_id, changes)));
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setError("");
    try {
      await deleteProject(project.project_id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  return (
    <div className="space-y-4">
      <form onSubmit={save} className="space-y-4 rounded-lg border border-border bg-card px-5 py-4">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1">
            <label htmlFor={`${id}-name`} className="text-xs font-medium text-muted-foreground">Name *</label>
            <Input id={`${id}-name`} value={draft.name} maxLength={80} onChange={(e) => set("name")(e.target.value)} />
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-dir`} className="text-xs font-medium text-muted-foreground">Default directory</label>
            <Input
              id={`${id}-dir`}
              value={draft.workDir}
              onChange={(e) => set("workDir")(e.target.value)}
              placeholder="No default directory"
              className="font-mono"
            />
          </div>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-desc`} className="text-xs font-medium text-muted-foreground">Description</label>
          <textarea
            id={`${id}-desc`}
            value={draft.description}
            onChange={(e) => set("description")(e.target.value)}
            rows={2}
            className="w-full resize-none rounded-md border border-input bg-transparent px-3 py-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <ProjectBackgroundField id={`${id}-background`} value={draft.background} onChange={set("background")} />
        <ProjectHarnessOverrides
          idPrefix={id}
          planner={draft.plannerHarness}
          worker={draft.workerHarness}
          onPlannerChange={set("plannerHarness")}
          onWorkerChange={set("workerHarness")}
        />
        <ProjectExecutionFields idPrefix={id} draft={draft.execution} onChange={set("execution")} />
        {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
        <div className="flex flex-wrap items-center justify-end gap-2">
          {saved && !dirty && (
            <span role="status" className="mr-auto flex items-center gap-1 text-xs text-success">
              <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
              Saved; it applies to the next run
            </span>
          )}
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={!dirty || busy}
            onClick={() => {
              setDraft(draftOf(project));
              setError("");
            }}
          >
            Discard changes
          </Button>
          <Button type="submit" size="sm" disabled={!dirty || busy || tooLong}>
            {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            Save
          </Button>
        </div>
      </form>

      <section aria-labelledby={`${id}-delete`} className="rounded-lg border border-border bg-card px-5 py-4">
        <h2 id={`${id}-delete`} className="text-sm font-semibold">Delete project</h2>
        {project.task_count > 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">
            Only a project without tasks can be deleted; move or delete its {project.task_count}{" "}
            {project.task_count === 1 ? "task" : "tasks"} first.
          </p>
        ) : (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <p className="mr-auto text-xs text-muted-foreground">
              Removes the project with its knowledge and skills.
            </p>
            {confirmDelete ? (
              <>
                <span className="text-xs text-muted-foreground">Delete {project.name}?</span>
                <Button type="button" variant="destructive" size="sm" onClick={remove} disabled={busy}>
                  {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  Delete
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmDelete(false)}>
                  Cancel
                </Button>
              </>
            ) : (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setConfirmDelete(true)}
                className="text-destructive hover:bg-destructive/10 hover:text-destructive"
              >
                <Trash2 className="h-3.5 w-3.5" />
                Delete project
              </Button>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
