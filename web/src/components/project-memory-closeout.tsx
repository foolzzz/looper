import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { updateMemoryCloseout, type MemoryCloseoutMode, type Project } from "@/lib/api";

/** This switch is saved through operator-only operations, separately from ordinary project fields. */
export function ProjectMemoryCloseout({ project }: { project: Project }) {
  const id = useId();
  const [stored, setStored] = useState<MemoryCloseoutMode>(project.memory_closeout ?? "off");
  const [mode, setMode] = useState(stored);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const lastSaved = useRef<MemoryCloseoutMode | null>(null);
  useEffect(() => {
    const value = project.memory_closeout ?? "off";
    setStored(value);
    setMode(value);
    setSaved(value === lastSaved.current);
    setError("");
  }, [project.project_id, project.memory_closeout]);

  const save = async () => {
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const result = await updateMemoryCloseout(project.project_id, mode);
      lastSaved.current = result.memory_closeout;
      setStored(result.memory_closeout);
      setMode(result.memory_closeout);
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <fieldset className="space-y-3 rounded-md border border-border px-3 py-3">
      <legend className="px-1 text-xs font-medium text-muted-foreground">Memory proposals at task end</legend>
      <div className="space-y-1">
        <label htmlFor={id} className="text-xs font-medium text-muted-foreground">Memory proposals at task end</label>
        <select id={id} value={mode} disabled={busy} aria-describedby={`${id}-help`}
          onChange={(event) => { setMode(event.target.value as MemoryCloseoutMode); setSaved(false); }}
          className="flex h-9 pointer-coarse:h-11 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground">
          <option value="off">Off (default)</option>
          <option value="on">On</option>
        </select>
        <p id={`${id}-help`} className="text-xs text-muted-foreground">
          When on, an ended task&apos;s Planner proposes lessons and project facts for you to confirm into your knowledge vault, instead of writing vault notes. Takes effect only while Settings → Knowledge vault → Memory writing has the tasks source on.
        </p>
      </div>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {saved && mode === stored && <span role="status" className="mr-auto text-xs text-success">Memory closeout setting saved</span>}
        <Button type="button" size="sm" variant="outline" disabled={busy || mode === stored} onClick={save}>
          {busy ? "Saving…" : "Save memory closeout"}
        </Button>
      </div>
    </fieldset>
  );
}
