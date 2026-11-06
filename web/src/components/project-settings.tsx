import { useId, useState } from "react";
import { CheckCircle2, Loader2, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  deleteProject, moveProjectDirectory, previewProjectMove, updateProject,
  type Project, type ProjectMovePreview, type ProjectMoveResult, type ProjectUpdate,
} from "@/lib/api";
import { ProjectBackgroundField, backgroundTooLong } from "@/components/project-background-field";
import type { V1PendingConfirmation } from "@/lib/v1";
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
  execution: ExecutionDraft;
}

function draftOf(project: Project): Draft {
  return {
    name: project.name,
    workDir: project.work_dir,
    description: project.description,
    background: project.background,
    execution: executionDraft(project),
  };
}

/** What the draft changes, as the API takes it; an empty directory or background clears it. */
function changesOf(draft: Draft, project: Project): ProjectUpdate {
  const fields: ProjectUpdate = executionChanges(draft.execution, project);
  if (draft.name.trim() !== project.name) fields.name = draft.name.trim();
  if (draft.description.trim() !== project.description) fields.description = draft.description.trim();
  if (draft.workDir.trim() !== project.work_dir) fields.work_dir = draft.workDir.trim();
  if (draft.background !== project.background) fields.background = draft.background;
  return fields;
}

function ProjectDirectoryMove({ project, projectsRoot, disabled, unsaved, onBusy, onMoved }: {
  project: Project; projectsRoot: string; disabled: boolean; unsaved: boolean;
  onBusy: (busy: boolean) => void; onMoved: (project: Project) => void;
}) {
  const id = useId();
  const [to, setTo] = useState("");
  const [linkOld, setLinkOld] = useState(true);
  const [preview, setPreview] = useState<ProjectMovePreview | null>(null);
  const [result, setResult] = useState<ProjectMoveResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const placeholder = projectsRoot ? `Leave empty for a new directory under ${projectsRoot}` : "/absolute/path";
  const fields = { ...(to.trim() ? { to: to.trim() } : {}), link_old: linkOld };
  const run = async (confirmed: boolean) => {
    if (to.trim() && !to.trim().startsWith("/")) return setError("The target must be an absolute path.");
    setBusy(true);
    onBusy(true);
    setError("");
    setResult(null);
    try {
      if (confirmed && preview) {
        const moved = await moveProjectDirectory(project.project_id, fields, preview.move_version);
        setResult(moved);
        setPreview(null);
        onMoved(moved.project);
      } else {
        setPreview(await previewProjectMove(project.project_id, fields));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPreview(null);
    } finally {
      setBusy(false);
      onBusy(false);
    }
  };
  return (
    <section aria-labelledby={`${id}-move`} className="space-y-3 rounded-lg border border-border bg-card px-5 py-4">
      <h2 id={`${id}-move`} className="text-sm font-semibold">Move directory</h2>
      <p className="text-xs text-muted-foreground">Moves the directory with all its files and updates the schedules that use it. Anything in progress in the project (a task step, a schedule run or a gate) blocks the move; the preview says what.</p>
      <div className="space-y-1">
        <label htmlFor={`${id}-to`} className="text-xs font-medium text-muted-foreground">Target directory (absolute path)</label>
        <Input id={`${id}-to`} value={to} disabled={disabled || busy} className="font-mono"
          placeholder={placeholder}
          onChange={(event) => { setTo(event.target.value); setPreview(null); setResult(null); }} />
      </div>
      <label className="flex items-center gap-2 text-xs">
        <Switch checked={linkOld} label="Keep a link at the old path" disabled={disabled || busy}
          onChange={(value) => { setLinkOld(value); setPreview(null); setResult(null); }} />
        Keep a link at the old path
      </label>
      {!project.work_dir && <p className="text-xs text-muted-foreground">Set the project's directory first.</p>}
      {unsaved && <p className="text-xs text-muted-foreground">Save or discard your changes to the settings before moving the directory.</p>}
      {preview && <div className="space-y-2 border-t border-border pt-3">
        <p className="whitespace-pre-wrap break-words text-xs">{preview.impact}</p>
      </div>}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {result && <div role="status" className="space-y-2 text-xs">
        <p className="break-all">Moved to {result.new_dir}.</p>
        <p className="break-words">{result.notes.moved ? "Notes moved" : `Notes not moved: ${result.notes.reason}`}</p>
        {result.repairs.filter((repair) => !repair.ok).map((repair) => (
          <p key={repair.repository} role="alert" className="whitespace-pre-wrap break-words text-destructive">
            Git worktree repair failed: {repair.repository}{"\n"}{repair.output}
          </p>
        ))}
      </div>}
      <div className="flex flex-wrap justify-end gap-2">
        {preview && <Button type="button" variant="ghost" size="sm" disabled={busy}
          onClick={() => setPreview(null)}>Cancel</Button>}
        <Button type="button" variant="outline" size="sm" disabled={disabled || busy || !project.work_dir || (!projectsRoot && !to.trim())}
          onClick={() => run(!!preview)}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          {busy ? "Working…" : preview ? "Move directory" : "Preview move"}
        </Button>
      </div>
    </section>
  );
}

/** A project's fields and execution settings, saved together, and deleting it with its tasks and schedules.
 * Its agent settings are on its Agent Profiles tab. */
export function ProjectSettingsForm({ project, projectsRoot = "", onDeleted }: { project: Project; projectsRoot?: string; onDeleted: () => void }) {
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => draftOf(project));
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<"local" | V1PendingConfirmation | null>(null);

  const set = <K extends keyof Draft>(key: K) => (value: Draft[K]) => {
    setDraft((d) => ({ ...d, [key]: value }));
    setSaved(false);
  };
  const changes = changesOf(draft, project);
  const dirty = Object.keys(changes).length > 0;
  const tooLong = backgroundTooLong(draft.background);

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
    if (!confirmDelete) return;
    setBusy(true);
    setDeleting(true);
    setError("");
    try {
      const pending = await deleteProject(project.project_id, confirmDelete === "local" ? undefined : confirmDelete);
      if (pending) setConfirmDelete(pending);
      else onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setConfirmDelete(null);
    } finally {
      setBusy(false);
      setDeleting(false);
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
            <p className="text-xs text-muted-foreground">Changes the stored path only; files stay where they are. To move the files, use Move directory below.</p>
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
            {busy && !deleting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            Save
          </Button>
        </div>
      </form>

      <ProjectDirectoryMove project={project} projectsRoot={projectsRoot} disabled={busy || dirty} unsaved={dirty} onBusy={setBusy}
        onMoved={(moved) => { setDraft(draftOf(moved)); setSaved(false); }} />

      <section aria-labelledby={`${id}-delete`} className="rounded-lg border border-border bg-card px-5 py-4">
        <h2 id={`${id}-delete`} className="text-sm font-semibold">Delete project</h2>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <p className="mr-auto text-xs text-muted-foreground">
            Removes the project with its knowledge and skills
            {project.task_count > 0 && <>, its {project.task_count} tasks ({project.active_count > 0 && <>the {project.active_count} running ones are stopped first; </>}result branches stay)</>}
            {project.schedule_count > 0 && <>, and its {project.schedule_count} schedules with their run history</>}.
          </p>
          {confirmDelete ? (
            <>
              <p className="w-full whitespace-pre-wrap break-words text-xs text-muted-foreground">
                {confirmDelete === "local" ? `Delete ${project.name}? This cannot be undone.` : confirmDelete.impact}
              </p>
              <Button type="button" variant="destructive" size="sm" onClick={remove} disabled={busy}>
                {deleting && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
                {deleting ? "Stopping and deleting…" : "Delete"}
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmDelete(null)} disabled={busy}>
                Cancel
              </Button>
            </>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setConfirmDelete("local")}
              disabled={busy}
              className="text-destructive hover:bg-destructive/10 hover:text-destructive"
            >
              <Trash2 className="h-3.5 w-3.5" />
              Delete project
            </Button>
          )}
        </div>
      </section>
    </div>
  );
}
