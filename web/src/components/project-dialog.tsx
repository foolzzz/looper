import { useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { createProject, type Project } from "@/lib/api";
import { ProjectBackgroundField, backgroundTooLong } from "@/components/project-background-field";

interface ProjectDialogProps {
  onClose: () => void;
  onCreated: (project: Project) => void;
}

/** Create a project: a name, an optional default directory, description and background. Its agent settings are
 * on its Agent Profiles tab. */
export function ProjectDialog({ onClose, onCreated }: ProjectDialogProps) {
  const id = useId();
  const [name, setName] = useState("");
  const [workDir, setWorkDir] = useState("");
  const [description, setDescription] = useState("");
  const [background, setBackground] = useState("");
  const [saving, setSaving] = useState(false);
  const tooLong = backgroundTooLong(background);
  const [error, setError] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || saving || tooLong) return;
    setSaving(true);
    setError("");
    try {
      const project = await createProject({
        name: name.trim(),
        description: description.trim() || undefined,
        work_dir: workDir.trim() || undefined,
        background: background.trim() ? background : undefined,
      });
      onCreated(project);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        onSubmit={submit}
        className="max-h-full w-full max-w-md space-y-4 overflow-y-auto rounded-xl border border-border bg-popover p-6 text-popover-foreground shadow-xl"
      >
        <h2 id={`${id}-title`} className="text-lg font-semibold">New project</h2>
        <div className="space-y-1">
          <label htmlFor={`${id}-name`} className="text-sm font-medium text-muted-foreground">Name *</label>
          <Input
            id={`${id}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={80}
            placeholder="my-app"
            autoFocus
          />
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-dir`} className="text-sm font-medium text-muted-foreground">Default directory</label>
          <Input
            id={`${id}-dir`}
            value={workDir}
            onChange={(e) => setWorkDir(e.target.value)}
            placeholder="/path/to/project"
            className="font-mono"
          />
          <p className="text-xs text-muted-foreground">
            Absolute path. New tasks run here unless they set their own directory.
          </p>
        </div>
        <div className="space-y-1">
          <label htmlFor={`${id}-desc`} className="text-sm font-medium text-muted-foreground">Description</label>
          <textarea
            id={`${id}-desc`}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={2}
            className="w-full resize-none rounded-md border border-input bg-transparent px-3 py-2 text-sm text-foreground shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <ProjectBackgroundField
          id={`${id}-background`}
          value={background}
          onChange={setBackground}
          rows={4}
          labelClassName="text-sm"
        />
        {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={!name.trim() || saving || tooLong}>
            {saving && <Loader2 className="h-4 w-4 animate-spin" />}
            Create
          </Button>
        </div>
      </form>
    </div>
  );
}
