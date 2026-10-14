import { useEffect, useId, useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  addProjectSkill,
  deleteProjectSkill,
  fetchProjectSkills,
  setProjectSkillEnabled,
  type ProjectSkill,
  type ProjectSkills,
} from "@/lib/api";
import { cn, formatBytes } from "@/lib/utils";

function SkillRow({
  skill,
  busy,
  onToggle,
  onDelete,
}: {
  skill: ProjectSkill;
  busy: boolean;
  onToggle: (enabled: boolean) => void;
  onDelete: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  return (
    <li className="space-y-1 border-t border-border py-2 first:border-t-0">
      {/* In a narrow column the switch and the delete button wrap below the text instead of squeezing it. */}
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <div className={cn("min-w-0 flex-[1_1_10rem] space-y-0.5", !skill.enabled && "opacity-60")}>
          <p className="flex flex-wrap items-baseline gap-x-2 font-mono text-xs">
            <span className="break-all font-medium">{skill.qualified_name}</span>
            <span className="text-[11px] tabular-nums text-muted-foreground">{formatBytes(skill.size_bytes)}</span>
            {!skill.enabled && <span className="font-sans text-[11px] text-muted-foreground">Off</span>}
          </p>
          <p className="break-words text-xs text-muted-foreground">{skill.description}</p>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          <Switch checked={skill.enabled} disabled={busy} onChange={onToggle} label={`Load ${skill.name}`} />
          {!confirming && (
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
          )}
        </div>
      </div>
      {confirming && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="text-xs text-muted-foreground">Delete this skill?</span>
          <Button type="button" variant="destructive" size="sm" onClick={onDelete} disabled={busy}>
            Delete
          </Button>
          <Button type="button" variant="ghost" size="sm" onClick={() => setConfirming(false)}>
            Cancel
          </Button>
        </div>
      )}
    </li>
  );
}

/**
 * The project's own Claude Code skills, managed in the project editor: only its Planner, Workers and Reviewers load
 * them, from their next run on. Adding, switching and deleting apply at once, apart from the editor's Save.
 */
export function ProjectSkillsSection({ projectId }: { projectId: string }) {
  const id = useId();
  const [data, setData] = useState<ProjectSkills | null>(null);
  const [loadError, setLoadError] = useState("");
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    fetchProjectSkills(projectId)
      .then((view) => alive && setData(view))
      .catch((err) => alive && setLoadError(err instanceof Error ? err.message : String(err)));
    return () => {
      alive = false;
    };
  }, [projectId]);

  const startAdding = () => {
    setAdding(true);
    setName("");
    setDescription("");
    setContent("");
    setError("");
  };

  /** Run one change; the view it answers with replaces the list, and a refusal shows the server's reason. */
  const apply = async (change: () => Promise<ProjectSkills>, after?: () => void) => {
    setBusy(true);
    setError("");
    try {
      setData(await change());
      after?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    void apply(
      () => addProjectSkill(projectId, { name: name.trim(), description: description.trim(), content }),
      () => setAdding(false),
    );
  };

  const caps = data?.caps;
  const full = data && caps ? data.count >= caps.project_max_skills : false;
  const ready = name.trim() && description.trim() && content.trim();

  return (
    <section aria-labelledby={`${id}-title`} className="space-y-2 border-t border-border pt-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 id={`${id}-title`} className="text-xs font-medium text-muted-foreground">Skills</h3>
        {data && caps && (
          <span className="text-[11px] tabular-nums text-muted-foreground">
            {data.count} / {caps.project_max_skills} skills · {data.enabled_count} on
          </span>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        Claude Code skills that only this project's Planner, Workers and Reviewers load, as{" "}
        <span className="font-mono">{data?.plugin ?? "project"}:&lt;name&gt;</span>. Nothing is written to the
        project's directory. Changes apply to their next run, without Save.
      </p>
      {loadError && <p role="alert" className="text-xs text-destructive">{loadError}</p>}
      {!data && !loadError && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading" />}
      {data &&
        (data.skills.length ? (
          <ul className="rounded-md border border-border px-3">
            {data.skills.map((skill) => (
              <SkillRow
                key={skill.skill_id}
                skill={skill}
                busy={busy}
                onToggle={(enabled) => apply(() => setProjectSkillEnabled(projectId, skill.skill_id, enabled))}
                onDelete={() => apply(() => deleteProjectSkill(projectId, skill.skill_id))}
              />
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No skills</p>
        ))}
      {data && !adding && (
        <Button type="button" variant="outline" size="sm" onClick={startAdding} disabled={full}>
          <Plus className="h-3.5 w-3.5" />
          Skill
        </Button>
      )}
      {adding && (
        <form onSubmit={submit} className="space-y-2 rounded-md border border-border p-3">
          <p className="flex items-center gap-1.5 text-xs font-medium">
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            New skill
          </p>
          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
            <div className="space-y-1">
              <label htmlFor={`${id}-skill-name`} className="text-xs font-medium text-muted-foreground">Name *</label>
              <Input
                id={`${id}-skill-name`}
                value={name}
                maxLength={caps?.name_max_chars ?? 64}
                onChange={(e) => setName(e.target.value)}
                placeholder="deploy-check"
                spellCheck={false}
                autoCapitalize="none"
                aria-describedby={`${id}-skill-name-hint`}
                className="font-mono"
                autoFocus
              />
              <p id={`${id}-skill-name-hint`} className="text-xs text-muted-foreground">
                Lowercase letters, digits and hyphens.
              </p>
            </div>
            <div className="space-y-1">
              <label htmlFor={`${id}-skill-description`} className="text-xs font-medium text-muted-foreground">
                Description *
              </label>
              <Input
                id={`${id}-skill-description`}
                value={description}
                maxLength={caps?.description_max_chars ?? 1024}
                onChange={(e) => setDescription(e.target.value)}
                aria-describedby={`${id}-skill-description-hint`}
              />
              <p id={`${id}-skill-description-hint`} className="text-xs text-muted-foreground">
                One line on what it does and when to use it; agents choose skills by it.
              </p>
            </div>
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-skill-content`} className="text-xs font-medium text-muted-foreground">
              Instructions (SKILL.md body) *
            </label>
            <textarea
              id={`${id}-skill-content`}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              rows={8}
              spellCheck={false}
              aria-describedby={`${id}-skill-content-hint`}
              className="w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            />
            <p id={`${id}-skill-content-hint`} className="text-xs text-muted-foreground">
              Markdown without frontmatter; the name and description make it. At most{" "}
              {caps ? formatBytes(caps.content_max_bytes) : "64 KB"}.
            </p>
          </div>
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
    </section>
  );
}
