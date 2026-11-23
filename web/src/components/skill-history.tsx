import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  fetchSkillVersions,
  rollbackProjectSkill,
  type SkillAuthorKind,
  type SkillVersion,
  type SkillVersionDiff,
  type SkillVersions,
} from "@/lib/api";

const AUTHOR_LABEL: Record<SkillAuthorKind, string> = {
  operator: "Operator",
  proposal: "Proposal",
  rollback: "Rollback",
};

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

function authorLabel(version: SkillVersion): string {
  const label = AUTHOR_LABEL[version.author_kind];
  if (version.author_kind === "rollback" && version.author_ref) return `${label} from version ${version.author_ref}`;
  return label;
}

function timeLabel(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

interface Preview {
  from: number;
  diff: SkillVersionDiff;
  affected: SkillVersions["affected_projects"];
}

/**
 * Versions of one skill written in this project, and a rollback that writes the chosen version as a new one.
 * The difference and the projects that install the skill are shown before the write.
 */
export function SkillHistory({
  id,
  projectId,
  skillId,
  revision,
  busy,
  onRolledBack,
}: {
  id: string;
  projectId: string;
  skillId: string;
  /** Changes when the skill is saved, so the list is read again. */
  revision: string;
  busy: boolean;
  onRolledBack: () => Promise<boolean> | void;
}) {
  const [history, setHistory] = useState<SkillVersions | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let alive = true;
    setError("");
    setPreview(null);
    fetchSkillVersions(projectId, skillId)
      .then((data) => alive && setHistory(data))
      .catch((err) => alive && setError(errorText(err)));
    return () => {
      alive = false;
    };
  }, [projectId, skillId, revision]);

  const previewRollback = async (version: number) => {
    if (history?.current_version == null) return;
    setError("");
    try {
      // The difference is what Confirm writes: the current version, then the chosen one.
      const data = await fetchSkillVersions(projectId, skillId, {
        fromVersion: history.current_version,
        toVersion: version,
      });
      if (!data.diff) {
        setError("The server returned no difference.");
        return;
      }
      setPreview({ from: version, diff: data.diff, affected: data.affected_projects });
    } catch (err) {
      setError(errorText(err));
    }
  };

  const confirm = async () => {
    if (!preview) return;
    setSaving(true);
    setError("");
    try {
      await rollbackProjectSkill(projectId, skillId, preview.from);
      setPreview(null);
      await onRolledBack();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const held = busy || saving;
  const nameChanged = preview != null && preview.diff.name.from !== preview.diff.name.to;
  const descriptionChanged = preview != null && preview.diff.description.from !== preview.diff.description.to;

  return (
    <div id={id} className="ml-5 min-w-0 space-y-2" aria-label="Version history">
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      {!history && !error && (
        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-label="Loading versions" />
      )}
      {history && history.versions.length === 0 && (
        <p className="text-xs text-muted-foreground">This skill has no saved versions.</p>
      )}
      {history && history.versions.length > 0 && (
        <ul className="space-y-1">
          {history.versions.map((version) => {
            const current = history.current_version === version.version;
            return (
              <li key={version.version} className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1 text-xs">
                <span className="font-mono">Version {version.version}</span>
                <span>{authorLabel(version)}</span>
                <time dateTime={version.created_at} className="text-muted-foreground">{timeLabel(version.created_at)}</time>
                {version.reason && <span className="min-w-0 break-words text-muted-foreground">{version.reason}</span>}
                {current ? (
                  <Badge variant="outline">Current</Badge>
                ) : history.current_version != null ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={held}
                    aria-label={`Roll back to version ${version.version}`}
                    onClick={() => void previewRollback(version.version)}
                  >Roll back</Button>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {preview && (
        <div className="min-w-0 space-y-2 rounded-md border border-border p-3">
          {nameChanged && (
            <p className="break-words text-xs">
              Name: <span className="font-mono">{preview.diff.name.from}</span>
              {" → "}
              <span className="font-mono">{preview.diff.name.to}</span>
            </p>
          )}
          {descriptionChanged && (
            <p className="break-words text-xs">
              Description: {preview.diff.description.from} → {preview.diff.description.to}
            </p>
          )}
          <pre className="max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground/90">
            {preview.diff.content}
          </pre>
          <p className="break-words text-xs text-muted-foreground">
            {preview.affected.length === 0
              ? "No project installs this skill."
              : `Installed in ${preview.affected.map((project) => project.project_name).join(", ")}.`}
          </p>
          <div className="flex flex-wrap justify-end gap-2">
            <Button type="button" size="sm" disabled={held} onClick={() => void confirm()}>
              {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              {"Confirm rollback"}
            </Button>
            <Button type="button" variant="ghost" size="sm" disabled={held} onClick={() => setPreview(null)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
