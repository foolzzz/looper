import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { editTaskRequirement, type RequirementVersion, type TaskDetail } from "@/lib/api";
import { errorText } from "@/lib/use-action";
import { isMissingOperation } from "@/lib/work";

/**
 * Statuses whose requirement the server accepts an edit for: a draft, a task that has not started,
 * and one that is running or starting. Every other status, including an unknown one, is read-only.
 */
const REQUIREMENT_EDITABLE = new Set([
  "draft",
  "init",
  "assigned",
  "running",
  "planning",
  "executing",
  "verifying",
  "stopping",
  "waiting_user",
  "awaiting_budget",
  "waiting",
  "pending",
]);

const FIELD = "mt-2 w-full min-w-0 rounded-md border border-border bg-background px-3 py-2 text-sm pointer-coarse:min-h-11 pointer-coarse:text-base focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring";
const BODY = "whitespace-pre-wrap break-words [overflow-wrap:anywhere]";

function requirementText(task: TaskDetail): string {
  return typeof task.requirement === "string" ? task.requirement : "";
}

function versionsOf(task: TaskDetail): RequirementVersion[] {
  return Array.isArray(task.requirement_versions)
    ? [...task.requirement_versions].sort((a, b) => a.version - b.version)
    : [];
}

function Delivery({ version }: { version: RequirementVersion }) {
  if (version.delivered_at) return <> · <time dateTime={version.delivered_at}>{version.delivered_at}</time></>;
  if (version.version > 1) return <> · 规划者尚未读取</>;
  return null;
}

/** The Work column's requirement. It renders the task it is given and asks the page to read that task back after a save. */
export function WorkRequirement({ task, onRefresh }: { task: TaskDetail; onRefresh: () => Promise<void> }) {
  const body = requirementText(task);
  const versions = versionsOf(task);
  const current = versions.length > 0 ? versions[versions.length - 1] : null;
  const history = versions.length > 1 ? [...versions].reverse() : [];
  const editable = REQUIREMENT_EDITABLE.has(task.status);
  const [taskKey, setTaskKey] = useState(task.task_id);
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(body);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [unavailable, setUnavailable] = useState(false);
  const ticket = useRef(0);
  const savingRef = useRef(false);
  const mounted = useRef(false);
  const taskIdRef = useRef(task.task_id);
  taskIdRef.current = task.task_id;
  if (task.task_id !== taskKey) {
    setTaskKey(task.task_id);
    setEditing(false);
    setText(body);
    setError("");
    setSaving(false);
    savingRef.current = false;
    ticket.current += 1;
  }
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const begin = () => {
    if (unavailable) return;
    setText(body);
    setError("");
    setEditing(true);
  };
  const cancel = () => {
    ticket.current += 1;
    savingRef.current = false;
    setSaving(false);
    setEditing(false);
    setText(body);
    setError("");
  };
  const save = () => {
    if (unavailable || savingRef.current) return;
    const requirement = text;
    if (!requirement.trim() || requirement === body) return;
    const id = task.task_id;
    const generation = ticket.current;
    const refresh = onRefresh;
    savingRef.current = true;
    setSaving(true);
    setError("");
    void (async () => {
      try {
        await editTaskRequirement(id, requirement);
        if (taskIdRef.current !== id) return;
        try {
          await refresh();
        } catch (err) {
          if (!mounted.current || taskIdRef.current !== id || ticket.current !== generation) return;
          setError(errorText(err));
          return;
        }
        if (!mounted.current || taskIdRef.current !== id || ticket.current !== generation) return;
        setEditing(false);
      } catch (err) {
        if (!mounted.current) return;
        if (isMissingOperation(err)) {
          setUnavailable(true);
          return;
        }
        if (taskIdRef.current !== id || ticket.current !== generation) return;
        setError(errorText(err));
      } finally {
        if (mounted.current && taskIdRef.current === id && ticket.current === generation) {
          savingRef.current = false;
          setSaving(false);
        }
      }
    })();
  };

  return (
    <section aria-labelledby="work-requirement-title" data-work-requirement="" className="min-w-0 p-4">
      <h2 id="work-requirement-title" className="text-sm font-semibold">需求</h2>
      {current && (
        <p data-requirement-version className="mt-1 text-xs text-muted-foreground">
          版本 {current.version}
          <Delivery version={current} />
        </p>
      )}
      {editing ? (
        <textarea aria-label="需求正文" value={text} rows={4} onChange={(event) => setText(event.target.value)} className={FIELD} />
      ) : body === "" ? (
        <p className="mt-2 text-sm text-muted-foreground">尚未填写需求</p>
      ) : (
        <p data-requirement-body className={`mt-2 text-sm ${BODY}`}>{body}</p>
      )}
      {editable && !unavailable && !editing && (
        <Button type="button" variant="outline" className="mt-2" onClick={begin}>编辑</Button>
      )}
      {editing && (
        <div className="mt-2 flex flex-wrap gap-2">
          <Button type="button" variant="outline" onClick={save} disabled={saving || unavailable || !text.trim() || text === body}>
            {saving ? "保存中…" : "保存"}
          </Button>
          <Button type="button" variant="outline" onClick={cancel}>取消</Button>
        </div>
      )}
      {unavailable && <p role="status" className="mt-2 text-sm text-muted-foreground">需求编辑暂不可用</p>}
      {error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
      {history.length > 0 && (
        <ol data-requirement-history className="mt-3 space-y-2">
          {history.map((version) => (
            <li key={version.version} data-version={version.version} className="rounded-md border border-border bg-card px-3 py-2">
              <p className="text-xs text-muted-foreground">
                版本 {version.version} · 来源 {version.via} · <time dateTime={version.created_at}>{version.created_at}</time>
                <Delivery version={version} />
              </p>
              <p className={`mt-1 text-sm ${BODY}`}>{version.text}</p>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
