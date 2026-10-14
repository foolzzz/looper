import { useEffect, useId, useRef, useState } from "react";
import { ChevronRight, FileUp, Loader2, Pencil, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  addProjectSkill,
  deleteProjectSkill,
  fetchProjectSkill,
  fetchProjectSkills,
  updateProjectSkill,
  type NewProjectSkill,
  type ProjectSkill,
  type ProjectSkills,
} from "@/lib/api";
import { parseSkillFile } from "@/lib/skill-file";
import { useFileDrop } from "@/lib/use-file-drop";
import { cn, formatBytes } from "@/lib/utils";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** The name, description and SKILL.md body of a skill being added or edited. */
function SkillFields({
  idPrefix,
  caps,
  draft,
  onChange,
}: {
  idPrefix: string;
  caps: ProjectSkills["caps"] | undefined;
  draft: NewProjectSkill;
  onChange: (draft: NewProjectSkill) => void;
}) {
  return (
    <>
      <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className="space-y-1">
          <label htmlFor={`${idPrefix}-name`} className="text-xs font-medium text-muted-foreground">Name *</label>
          <Input
            id={`${idPrefix}-name`}
            value={draft.name}
            maxLength={caps?.name_max_chars ?? 64}
            onChange={(e) => onChange({ ...draft, name: e.target.value })}
            placeholder="deploy-check"
            spellCheck={false}
            autoCapitalize="none"
            aria-describedby={`${idPrefix}-name-hint`}
            className="font-mono"
            autoFocus
          />
          <p id={`${idPrefix}-name-hint`} className="text-xs text-muted-foreground">
            Lowercase letters, digits and hyphens.
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${idPrefix}-description`} className="text-xs font-medium text-muted-foreground">
            Description *
          </label>
          <Input
            id={`${idPrefix}-description`}
            value={draft.description}
            maxLength={caps?.description_max_chars ?? 1024}
            onChange={(e) => onChange({ ...draft, description: e.target.value })}
            aria-describedby={`${idPrefix}-description-hint`}
          />
          <p id={`${idPrefix}-description-hint`} className="text-xs text-muted-foreground">
            One line on what it does and when to use it; agents choose skills by it.
          </p>
        </div>
      </div>
      <div className="space-y-1">
        <label htmlFor={`${idPrefix}-content`} className="text-xs font-medium text-muted-foreground">
          Instructions (SKILL.md body) *
        </label>
        <textarea
          id={`${idPrefix}-content`}
          value={draft.content}
          onChange={(e) => onChange({ ...draft, content: e.target.value })}
          rows={12}
          spellCheck={false}
          aria-describedby={`${idPrefix}-content-hint`}
          className="w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
        <p id={`${idPrefix}-content-hint`} className="text-xs text-muted-foreground">
          Markdown without frontmatter; the name and description make it. At most{" "}
          {caps ? formatBytes(caps.content_max_bytes) : "64 KB"}.
        </p>
      </div>
    </>
  );
}

function SkillRow({
  projectId,
  skill,
  caps,
  busy,
  onApply,
}: {
  projectId: string;
  skill: ProjectSkill;
  caps: ProjectSkills["caps"];
  busy: boolean;
  onApply: (change: () => Promise<ProjectSkills>) => Promise<boolean>;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [body, setBody] = useState<string | null>(null);
  const [bodyError, setBodyError] = useState("");
  const [draft, setDraft] = useState<NewProjectSkill>({ name: "", description: "", content: "" });

  const needBody = open || editing;
  useEffect(() => {
    if (!needBody) return;
    let alive = true;
    setBodyError("");
    fetchProjectSkill(projectId, skill.skill_id)
      .then((data) => alive && setBody(data.content))
      .catch((err) => alive && setBodyError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [needBody, projectId, skill.skill_id, skill.updated_at]);

  useEffect(() => {
    if (editing && body != null) setDraft((d) => (d.content ? d : { ...d, content: body }));
  }, [editing, body]);

  const startEdit = () => {
    setDraft({ name: skill.name, description: skill.description, content: body ?? "" });
    setConfirming(false);
    setEditing(true);
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    const fields: Partial<NewProjectSkill> = {};
    if (draft.name.trim() !== skill.name) fields.name = draft.name.trim();
    if (draft.description.trim() !== skill.description) fields.description = draft.description.trim();
    if (body != null && draft.content !== body) fields.content = draft.content;
    if (Object.keys(fields).length === 0) return setEditing(false);
    if (await onApply(() => updateProjectSkill(projectId, skill.skill_id, fields))) setEditing(false);
  };

  if (editing) {
    const loadingBody = body == null && !bodyError;
    return (
      <li className="border-t border-border py-3 first:border-t-0">
        <form onSubmit={save} className="space-y-2" aria-label={`Edit ${skill.name}`}>
          {loadingBody ? (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading the skill" />
          ) : (
            <SkillFields idPrefix={id} caps={caps} draft={draft} onChange={setDraft} />
          )}
          {bodyError && <p role="alert" className="text-xs text-destructive">{bodyError}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setEditing(false)}>Cancel</Button>
            <Button
              type="submit"
              size="sm"
              disabled={busy || loadingBody || !draft.name.trim() || !draft.description.trim() || !draft.content.trim()}
            >
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Save
            </Button>
          </div>
        </form>
      </li>
    );
  }

  return (
    <li className="space-y-2 border-t border-border py-2 first:border-t-0">
      {/* In a narrow column the switch and the buttons wrap below the text instead of squeezing it. */}
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          aria-controls={`${id}-body`}
          className={cn(
            "group flex min-w-0 flex-[1_1_10rem] items-start gap-1.5 rounded-md text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
            !skill.enabled && "opacity-60",
          )}
        >
          <ChevronRight
            className={cn(
              "mt-0.5 h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-150 motion-reduce:transition-none",
              open && "rotate-90",
            )}
            aria-hidden="true"
          />
          <span className="min-w-0 flex-1 space-y-0.5">
            <span className="flex flex-wrap items-baseline gap-x-2 font-mono text-xs">
              <span className="break-all font-medium group-hover:underline">{skill.qualified_name}</span>
              <span className="text-[11px] tabular-nums text-muted-foreground">{formatBytes(skill.size_bytes)}</span>
              {!skill.enabled && <span className="font-sans text-[11px] text-muted-foreground">Off</span>}
            </span>
            <span className="block break-words text-xs text-muted-foreground">{skill.description}</span>
          </span>
        </button>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <Switch
            checked={skill.enabled}
            disabled={busy}
            onChange={(enabled) => onApply(() => updateProjectSkill(projectId, skill.skill_id, { enabled }))}
            label={`Load ${skill.name}`}
          />
          {!confirming && (
            <>
              <Button type="button" variant="ghost" size="sm" onClick={startEdit} aria-label={`Edit ${skill.name}`}>
                <Pencil className="h-3.5 w-3.5" />
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setConfirming(true)}
                aria-label={`Delete ${skill.name}`}
                className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </>
          )}
        </div>
      </div>
      {confirming && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="text-xs text-muted-foreground">Delete this skill?</span>
          <Button
            type="button"
            variant="destructive"
            size="sm"
            onClick={() => onApply(() => deleteProjectSkill(projectId, skill.skill_id))}
            disabled={busy}
          >
            Delete
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
        </div>
      )}
      {open && (
        <div id={`${id}-body`} className="ml-5">
          {bodyError ? (
            <p role="alert" className="text-xs text-destructive">{bodyError}</p>
          ) : body == null ? (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading the skill" />
          ) : (
            <pre className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground/90">
              {body}
            </pre>
          )}
        </div>
      )}
    </li>
  );
}

/**
 * The project's own Claude Code skills: only its Planner, Workers and Reviewers load them, from their next run on.
 * Opening a row shows its SKILL.md body; adding (also from a SKILL.md file), editing, switching and deleting apply
 * at once.
 */
export function ProjectSkillsSection({ projectId }: { projectId: string }) {
  const id = useId();
  const fileInput = useRef<HTMLInputElement>(null);
  const [data, setData] = useState<ProjectSkills | null>(null);
  const [loadError, setLoadError] = useState("");
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState<NewProjectSkill>({ name: "", description: "", content: "" });
  const [fromFile, setFromFile] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchProjectSkills(projectId)
      .then((view) => alive && setData(view))
      .catch((err) => alive && setLoadError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [projectId]);

  const startAdding = (start: NewProjectSkill = { name: "", description: "", content: "" }, source = "") => {
    setAdding(true);
    setDraft(start);
    setFromFile(source);
    setError("");
  };

  /** A SKILL.md opens the add form with its frontmatter's name and description and the body below it. */
  const readSkillFile = async (file: File) => {
    const cap = data?.caps.content_max_bytes ?? 64 * 1024;
    // The frontmatter is not part of the body, so allow it some room over the body's cap.
    if (file.size > cap + 8 * 1024) {
      setError(`A skill's body has at most ${formatBytes(cap)}; ${file.name} has ${formatBytes(file.size)}.`);
      return;
    }
    startAdding(parseSkillFile(await file.text()), file.name);
  };

  const full = data ? data.count >= data.caps.project_max_skills : false;
  const { dragging, dropProps } = useFileDrop((file) => void readSkillFile(file), !!data && !full);

  /** Run one change; the view it answers with replaces the list, and a refusal shows the server's reason. */
  const apply = async (change: () => Promise<ProjectSkills>) => {
    setBusy(true);
    setError("");
    try {
      setData(await change());
      return true;
    } catch (err) {
      setError(errorText(err));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const skill = { name: draft.name.trim(), description: draft.description.trim(), content: draft.content };
    if (await apply(() => addProjectSkill(projectId, skill))) setAdding(false);
  };

  const caps = data?.caps;
  const ready = draft.name.trim() && draft.description.trim() && draft.content.trim();

  return (
    <section
      aria-labelledby={`${id}-title`}
      {...dropProps}
      className={cn(
        "relative space-y-3 rounded-lg border border-border bg-card px-5 py-4 transition-colors",
        dragging && "border-primary bg-primary/5",
      )}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id={`${id}-title`} className="text-sm font-semibold">Skills</h2>
        {data && caps && (
          <span className="text-[11px] tabular-nums text-muted-foreground">
            {data.count} / {caps.project_max_skills} skills · {data.enabled_count} on
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Claude Code skills that only this project&apos;s Planner, Workers and Reviewers load, as{" "}
        <span className="font-mono">{data?.plugin ?? "project"}:&lt;name&gt;</span>. Nothing is written to the
        project&apos;s directory. Changes apply to their next run.
      </p>
      {loadError && <p role="alert" className="text-xs text-destructive">{loadError}</p>}
      {!data && !loadError && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />}
      {data && caps &&
        (data.skills.length ? (
          <ul className="rounded-md border border-border px-3">
            {data.skills.map((skill) => (
              <SkillRow
                key={skill.skill_id}
                projectId={projectId}
                skill={skill}
                caps={caps}
                busy={busy}
                onApply={apply}
              />
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No skills</p>
        ))}
      {data && !adding && (
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="sm" onClick={() => startAdding()} disabled={full}>
            <Plus className="h-3.5 w-3.5" />
            Skill
          </Button>
          <Button type="button" variant="outline" size="sm" onClick={() => fileInput.current?.click()} disabled={full}>
            <FileUp className="h-3.5 w-3.5" />
            Upload SKILL.md
          </Button>
          {!full && <span className="text-xs text-muted-foreground">or drop a SKILL.md here</span>}
          <input
            ref={fileInput}
            type="file"
            accept=".md,text/markdown,text/plain"
            className="hidden"
            tabIndex={-1}
            aria-hidden="true"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void readSkillFile(file);
            }}
          />
        </div>
      )}
      {adding && (
        <form onSubmit={submit} className="space-y-2 rounded-md border border-border p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            New skill{fromFile && <span className="font-normal text-muted-foreground"> · from {fromFile}; check the fields</span>}
          </p>
          <SkillFields idPrefix={`${id}-skill`} caps={caps} draft={draft} onChange={setDraft} />
          {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setAdding(false)}>Cancel</Button>
            <Button type="submit" size="sm" disabled={busy || !ready}>
              {busy && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Add skill
            </Button>
          </div>
        </form>
      )}
      {!adding && error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {full && !adding && caps && (
        <p className="text-xs text-muted-foreground">A project has at most {caps.project_max_skills} skills.</p>
      )}
      {dragging && (
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 flex items-center justify-center rounded-lg bg-background/70 text-sm font-medium text-primary"
        >
          Drop to add it as a skill
        </div>
      )}
    </section>
  );
}
