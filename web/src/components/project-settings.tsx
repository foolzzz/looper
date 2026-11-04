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

function ProjectDirectoryMove({ project, disabled, unsaved, onBusy, onMoved }: {
  project: Project; disabled: boolean; unsaved: boolean;
  onBusy: (busy: boolean) => void; onMoved: (project: Project) => void;
}) {
  const id = useId();
  const [to, setTo] = useState("");
  const [linkOld, setLinkOld] = useState(true);
  const [preview, setPreview] = useState<ProjectMovePreview | null>(null);
  const [result, setResult] = useState<ProjectMoveResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const fields = { ...(to.trim() ? { to: to.trim() } : {}), link_old: linkOld };
  const run = async (confirmed: boolean) => {
    if (to.trim() && !to.trim().startsWith("/")) return setError("目标目录必须是绝对路径。");
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
      <h2 id={`${id}-move`} className="text-sm font-semibold">移动项目目录</h2>
      <p className="text-xs text-muted-foreground">移动目录及全部文件，并同步更新相关定时任务的目录。进行中的任务、命令或关卡会阻止移动。</p>
      <div className="space-y-1">
        <label htmlFor={`${id}-to`} className="text-xs font-medium text-muted-foreground">目标目录（绝对路径）</label>
        <Input id={`${id}-to`} value={to} disabled={disabled || busy} className="font-mono"
          placeholder="留空使用已配置的 projects root"
          onChange={(event) => { setTo(event.target.value); setPreview(null); setResult(null); }} />
      </div>
      <label className="flex items-center gap-2 text-xs">
        <Switch checked={linkOld} label="保留旧路径的符号链接" disabled={disabled || busy}
          onChange={(value) => { setLinkOld(value); setPreview(null); setResult(null); }} />
        保留旧路径的符号链接
      </label>
      {!project.work_dir && <p className="text-xs text-muted-foreground">请先为项目设置目录。</p>}
      {unsaved && <p className="text-xs text-muted-foreground">请先保存或放弃项目设置的修改，再移动目录。</p>}
      {preview && <div className="space-y-2 border-t border-border pt-3">
        <p className="whitespace-pre-wrap break-words text-xs">{preview.impact}</p>
      </div>}
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {result && <div role="status" className="space-y-2 text-xs">
        <p className="break-all">目录已移动到 {result.new_dir}。</p>
        {result.repairs.filter((repair) => !repair.ok).map((repair) => (
          <p key={repair.repository} role="alert" className="whitespace-pre-wrap break-words text-destructive">
            Git worktree 修复失败：{repair.repository}{"\n"}{repair.output}
          </p>
        ))}
      </div>}
      <div className="flex flex-wrap justify-end gap-2">
        {preview && <Button type="button" variant="ghost" size="sm" disabled={busy}
          onClick={() => setPreview(null)}>取消移动</Button>}
        <Button type="button" variant="outline" size="sm" disabled={disabled || busy || !project.work_dir}
          onClick={() => run(!!preview)}>
          {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
          {busy ? "处理中…" : preview ? "确认移动目录" : "预览移动"}
        </Button>
      </div>
    </section>
  );
}

/** A project's fields and execution settings, saved together, and deleting it with its tasks and schedules.
 * Its agent settings are on its Agent Profiles tab. */
export function ProjectSettingsForm({ project, onDeleted }: { project: Project; onDeleted: () => void }) {
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => draftOf(project));
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

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
    setBusy(true);
    setDeleting(true);
    setError("");
    try {
      await deleteProject(project.project_id);
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
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

      <ProjectDirectoryMove project={project} disabled={busy || dirty} unsaved={dirty} onBusy={setBusy}
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
              <span className="text-xs text-muted-foreground">
                Delete {project.name}{project.task_count > 0 && <> and its {project.task_count} tasks</>}?
              </span>
              <Button type="button" variant="destructive" size="sm" onClick={remove} disabled={busy}>
                {deleting && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />}
                {deleting ? "Stopping and deleting…" : "Delete"}
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setConfirmDelete(false)} disabled={busy}>
                Cancel
              </Button>
            </>
          ) : (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setConfirmDelete(true)}
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
