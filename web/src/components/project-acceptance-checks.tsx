import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { updateAcceptanceChecks, type AcceptanceChecksMode, type Project } from "@/lib/api";

/** This permission is saved through operator-only operations, separately from ordinary project fields. */
export function ProjectAcceptanceChecks({ project }: { project: Project }) {
  const id = useId();
  const [stored, setStored] = useState<AcceptanceChecksMode>(project.acceptance_checks ?? "off");
  const [mode, setMode] = useState(stored);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const lastSaved = useRef<AcceptanceChecksMode | null>(null);
  useEffect(() => {
    const value = project.acceptance_checks ?? "off";
    setStored(value);
    setMode(value);
    setSaved(value === lastSaved.current);
    setError("");
  }, [project.project_id, project.acceptance_checks]);

  const save = async () => {
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const result = await updateAcceptanceChecks(project.project_id, mode);
      lastSaved.current = result.acceptance_checks;
      setStored(result.acceptance_checks);
      setMode(result.acceptance_checks);
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <fieldset className="space-y-3 rounded-md border border-border px-3 py-3">
      <legend className="px-1 text-xs font-medium text-muted-foreground">Acceptance checks</legend>
      <div className="space-y-1">
        <label htmlFor={id} className="text-xs font-medium text-muted-foreground">Acceptance checks</label>
        <select id={id} value={mode} disabled={busy} aria-describedby={`${id}-help`}
          onChange={(event) => { setMode(event.target.value as AcceptanceChecksMode); setSaved(false); }}
          className="flex h-9 pointer-coarse:h-11 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm text-foreground">
          <option value="off">Off (default)</option>
          <option value="local">Local — no network</option>
          <option value="network">Network — only when a check requests it</option>
        </select>
        <p id={`${id}-help`} className="text-xs text-muted-foreground">
          Runs command and artifact checks after Worker success and stores evidence. Evidence does not affect decisions yet.
          Commands run as the server&apos;s user and can read that user&apos;s files;
          a private checkout is not a sandbox. Local denies network access. Network permits it only for checks that explicitly request it.
        </p>
      </div>
      {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {saved && mode === stored && <span role="status" className="mr-auto text-xs text-success">Acceptance checks setting saved</span>}
        <Button type="button" size="sm" variant="outline" disabled={busy || mode === stored} onClick={save}>
          {busy ? "Saving…" : "Save acceptance checks"}
        </Button>
      </div>
    </fieldset>
  );
}
