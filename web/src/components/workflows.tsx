import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { SELECT, EDIT_BOX } from "@/components/registry-ui";
import { deleteWorkflow, fetchWorkflows, previewWorkflow, putWorkflow, type Condition, type TaskRole, type Workflow } from "@/lib/api";
import type { V1PendingConfirmation } from "@/lib/v1";

export function useWorkflows() {
  const [rows, setRows] = useState<Workflow[]>([]);
  const [error, setError] = useState("");
  const reload = () => fetchWorkflows().then((data) => { setRows(data); setError(""); })
    .catch((err) => setError(String(err.message ?? err)));
  useEffect(() => { let alive = true;
    fetchWorkflows().then((data) => { if (alive) setRows(data); })
      .catch((err) => { if (alive) setError(String(err.message ?? err)); });
    return () => { alive = false; };
  }, []);
  return { rows, error, reload };
}

function passRule(condition: Condition): string {
  if (typeof condition === "string") return `${condition} passes`;
  if ("step" in condition) return `${condition.step} passes`;
  const items = "all_of" in condition ? condition.all_of : "any_of" in condition ? condition.any_of : condition.of;
  const rule = "all_of" in condition ? "all" : "any_of" in condition ? "any" : `${condition.k_of} of ${items.length}`;
  return `${rule} (${items.map(passRule).join(", ")})`;
}

export function WorkflowField({ rows, value, onChange, label = "Workflow", preview }: {
  rows: Workflow[]; value: string; onChange: (key: string) => void; label?: string;
  preview?: { projectId: string | null; profiles: Partial<Record<TaskRole, string>> };
}) {
  const selected = rows.find((row) => row.key === value);
  const request = selected && preview ? JSON.stringify({ key: value, projectId: preview.projectId,
    profiles: Object.fromEntries(Object.entries(preview.profiles).filter(([role, id]) => id && !selected.profiles[role as TaskRole])) }) : "";
  const [summary, setSummary] = useState<{ request: string; lines?: string[]; error?: string } | null>(null);
  useEffect(() => {
    if (!request) return;
    let alive = true;
    const { key, projectId, profiles } = JSON.parse(request);
    previewWorkflow(key, projectId, profiles).then((lines) => { if (alive) setSummary({ request, lines }); })
      .catch((err) => { if (alive) setSummary({ request, error: String(err.message ?? err) }); });
    return () => { alive = false; };
  }, [request, selected]);
  if (!rows.length) return null;
  const current = summary?.request === request ? summary : null;
  return <div className="space-y-1">
    <label className="text-sm font-medium text-muted-foreground" htmlFor="workflow-choice">{label}</label>
    <select id="workflow-choice" className={SELECT} value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="none">None</option>
      {rows.map((row) => <option value={row.key} key={row.key}>{row.name}</option>)}
    </select>
    {selected && preview && (current?.error ? <p role="alert" className="text-xs text-destructive">{current.error}</p>
      : <p className="whitespace-pre-wrap break-words text-xs text-muted-foreground">{current?.lines?.join("\n") ?? "正在读取工作流配置…"}</p>)}
    {selected && !preview && <p className="text-xs text-muted-foreground">Development: {selected.work_role ?? "default worker"}; review: {selected.review_panel
      ? `${selected.review_panel.members.join(", ")} · ${passRule(selected.review_panel.accept_when)}` : "default review"}; retries: {selected.max_step_retries ?? "project default"}; verification: {selected.verification ? `${selected.verification.role} (${selected.verification.max_rounds} rounds)` : "none"}.</p>}
  </div>;
}

export function WorkflowsSection() {
  const { rows, error, reload } = useWorkflows();
  const [draft, setDraft] = useState<string | null>(null);
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState<{ key: string; pending?: V1PendingConfirmation } | null>(null);
  const run = async (action: () => Promise<void>) => { setBusy(true); setFailure("");
    try { await action(); } catch (err) { setFailure(String((err as Error).message ?? err)); } finally { setBusy(false); }
  };
  return <div className="space-y-3">
    <Button onClick={() => { setDraft(JSON.stringify({ key: "", name: "" }, null, 2)); setFailure(""); }}>New workflow</Button>
    {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    {!rows.length && !error && <p className="text-sm text-muted-foreground">No workflows configured.</p>}
    {rows.map((row) => <div key={row.key} className="flex flex-wrap items-center gap-3 rounded-lg border border-border bg-card p-3">
      <div className="min-w-0 flex-1"><p className="font-medium">{row.name}</p><p className="text-xs text-muted-foreground">{row.key} · {row.description}</p></div>
      <Button variant="outline" onClick={() => { const { updated_at: _, ...definition } = row as Workflow & { updated_at?: string }; setDraft(JSON.stringify(definition, null, 2)); setFailure(""); }}>Edit</Button>
      <Button variant="ghost" onClick={() => { setDeleting({ key: row.key }); setFailure(""); }}>Delete</Button>
    </div>)}
    {draft !== null && <div className={EDIT_BOX}>
      <label htmlFor="workflow-json" className="text-sm font-medium">Workflow JSON</label>
      <textarea id="workflow-json" className="mt-2 min-h-64 w-full rounded-md border border-input bg-background p-3 font-mono text-sm" value={draft} onChange={(event) => setDraft(event.target.value)} />
      <div className="mt-2 flex gap-2"><Button disabled={busy} onClick={() => void run(async () => { await putWorkflow(JSON.parse(draft)); setDraft(null); await reload(); })}>Save</Button>
        <Button variant="ghost" disabled={busy} onClick={() => setDraft(null)}>Cancel</Button></div>
    </div>}
    {deleting && <div className={EDIT_BOX} role="alert">
      <p className="text-sm">{deleting.pending?.impact ?? `Delete ${deleting.key}? Project defaults will be cleared; existing tasks keep their workflow.`}</p>
      <div className="mt-2 flex gap-2"><Button disabled={busy} onClick={() => void run(async () => {
        const pending = await deleteWorkflow(deleting.key, deleting.pending);
        if (pending) setDeleting({ ...deleting, pending }); else { setDeleting(null); await reload(); }
      })}>Delete workflow</Button><Button disabled={busy} variant="ghost" onClick={() => setDeleting(null)}>Cancel</Button></div>
    </div>}
    {failure && <p role="alert" className="text-sm text-destructive">{failure}</p>}
  </div>;
}
