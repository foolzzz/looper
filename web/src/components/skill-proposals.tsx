import { useCallback, useEffect, useId, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import {
  confirmSkillProposal,
  fetchSkillProposals,
  rejectSkillProposal,
  restoreSkillProposal,
  setSkillProposalMode,
  type SkillProposal,
  type SkillProposalList,
} from "@/lib/api";
import { V1Error } from "@/lib/v1";

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err));

function timeLabel(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** A unified diff, one line each; the +/- sign is the signal, the colour only helps. */
function DiffText({ text }: { text: string }) {
  return (
    <pre className="max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground/90">
      {text.split("\n").map((line, index) => (
        <span
          key={index}
          className={line.startsWith("+") && !line.startsWith("+++") ? "block text-success" : line.startsWith("-") && !line.startsWith("---") ? "block text-destructive" : "block"}
        >
          {line}
        </span>
      ))}
    </pre>
  );
}

interface Edit {
  name: string;
  description: string;
  content: string;
}

function ProposalCard({
  projectId,
  proposal,
  busy,
  onChanged,
  onConflict,
  onWritten,
}: {
  projectId: string;
  proposal: SkillProposal;
  busy: boolean;
  onChanged: () => Promise<void>;
  onConflict: (message: string) => Promise<void>;
  onWritten: () => Promise<unknown> | void;
}) {
  const id = useId();
  const [mode, setMode] = useState<"view" | "reject" | "edit">("view");
  const [reason, setReason] = useState("");
  const [edit, setEdit] = useState<Edit>({ name: proposal.name, description: proposal.description, content: proposal.content });
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const held = busy || working;
  const update = proposal.action === "update";

  const run = async (work: () => Promise<unknown>, wrote = false) => {
    setWorking(true);
    setError("");
    try {
      await work();
      if (wrote) await onWritten();
      await onChanged();
    } catch (err) {
      // A 409 means what was shown is no longer what the server holds: read it again, and say so.
      if (err instanceof V1Error && err.status === 409) await onConflict(err.message);
      else setError(errorText(err));
    } finally {
      setWorking(false);
    }
  };

  const changed: Partial<Edit> = {
    ...(!update && edit.name.trim() !== proposal.name ? { name: edit.name.trim() } : {}),
    ...(edit.description.trim() !== proposal.description ? { description: edit.description.trim() } : {}),
    ...(edit.content !== proposal.content ? { content: edit.content } : {}),
  };
  const nameChange = update && proposal.diff && proposal.diff.name.from !== proposal.diff.name.to;
  const descriptionChange = update && proposal.diff && proposal.diff.description.from !== proposal.diff.description.to;

  return (
    <li className="min-w-0 space-y-2 border-t border-border py-3 first:border-t-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="break-all font-mono text-xs font-medium">{proposal.name}</span>
        <Badge variant="outline" className="py-0 text-[10px]">{update ? "Update" : "New skill"}</Badge>
        <span className="text-[11px] text-muted-foreground">{proposal.status === "expired" ? "Expired" : "Pending"}</span>
        <span className="text-[11px] text-muted-foreground">
          from {proposal.proposer} · {proposal.status === "expired" ? "expired" : "expires"}{" "}
          <time dateTime={proposal.expires_at}>{timeLabel(proposal.expires_at)}</time>
        </span>
      </div>
      <p className="break-words text-xs">{proposal.why}</p>
      {proposal.evidence.length > 0 && (
        <p className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          {proposal.evidence.map((entry) => (
            <a
              key={`${entry.task}/${entry.step ?? ""}`}
              href={`#tasks/${entry.task}`}
              className="underline-offset-2 hover:text-foreground hover:underline"
            >
              Task {entry.task}{entry.step ? `, step ${entry.step}` : ""}
            </a>
          ))}
        </p>
      )}
      {proposal.stale && (
        <p role="alert" className="text-xs text-destructive">
          {proposal.current
            ? "The skill changed after this was drafted. Confirming marks the proposal superseded and writes nothing."
            : "The skill no longer exists. Confirming marks the proposal superseded and writes nothing."}
        </p>
      )}
      {mode !== "edit" && (
        <div className="min-w-0 space-y-2">
          {!descriptionChange && <p className="break-words text-xs text-muted-foreground">Description: {proposal.description}</p>}
          {update && proposal.diff ? (
            <>
              {nameChange && proposal.diff && (
                <p className="break-words text-xs">Name: <span className="font-mono">{proposal.diff.name.from}</span> → <span className="font-mono">{proposal.diff.name.to}</span></p>
              )}
              {descriptionChange && proposal.diff && (
                <p className="break-words text-xs">Description: {proposal.diff.description.from} → {proposal.diff.description.to}</p>
              )}
              <DiffText text={proposal.diff.content || "The instructions are unchanged."} />
            </>
          ) : (
            <pre className="max-h-96 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-muted/40 p-3 font-mono text-xs leading-relaxed text-foreground/90">
              {proposal.content}
            </pre>
          )}
          {update && (
            <p className="break-words text-xs text-muted-foreground">
              {proposal.affected_projects.length === 0
                ? "No project installs this skill."
                : `A change reaches every project that installs it: ${proposal.affected_projects.map((p) => p.project_name).join(", ")}.`}
            </p>
          )}
        </div>
      )}
      {mode === "edit" && (
        <div className="space-y-2 rounded-md border border-border p-3">
          {!update && (
            <div className="space-y-1">
              <label htmlFor={`${id}-name`} className="text-xs font-medium text-muted-foreground">Name</label>
              <Input id={`${id}-name`} value={edit.name} spellCheck={false} autoCapitalize="none" className="font-mono"
                onChange={(e) => setEdit({ ...edit, name: e.target.value })} />
            </div>
          )}
          <div className="space-y-1">
            <label htmlFor={`${id}-description`} className="text-xs font-medium text-muted-foreground">Description</label>
            <Input id={`${id}-description`} value={edit.description}
              onChange={(e) => setEdit({ ...edit, description: e.target.value })} />
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-content`} className="text-xs font-medium text-muted-foreground">Instructions (SKILL.md body)</label>
            <textarea id={`${id}-content`} value={edit.content} rows={10} spellCheck={false}
              onChange={(e) => setEdit({ ...edit, content: e.target.value })}
              className="w-full resize-y rounded-md border border-input bg-transparent px-3 py-2 font-mono text-xs leading-relaxed text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring" />
          </div>
          <p className="text-xs text-muted-foreground">Saving writes your text as the skill, as a version you made.</p>
        </div>
      )}
      {mode === "reject" && (
        <div className="space-y-1">
          <label htmlFor={`${id}-reason`} className="text-xs font-medium text-muted-foreground">Reason (optional)</label>
          <Input id={`${id}-reason`} value={reason} maxLength={500} onChange={(e) => setReason(e.target.value)} />
        </div>
      )}
      {error && <p role="alert" className="break-words text-xs text-destructive">{error}</p>}
      <div className="flex flex-wrap justify-end gap-2">
        {proposal.status === "expired" ? (
          <Button type="button" variant="outline" size="sm" disabled={held}
            aria-label={`Restore proposal ${proposal.name}`}
            onClick={() => void run(() => restoreSkillProposal(projectId, proposal.proposal_id))}>
            {working && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            Restore
          </Button>
        ) : mode === "view" ? (
          <>
            <Button type="button" size="sm" disabled={held} aria-label={`Confirm proposal ${proposal.name}`}
              onClick={() => void run(() => confirmSkillProposal(projectId, proposal.proposal_id, proposal.content_sha256), true)}>
              {working && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Confirm
            </Button>
            <Button type="button" variant="outline" size="sm" disabled={held} aria-label={`Edit proposal ${proposal.name} then confirm`}
              onClick={() => setMode("edit")}>
              Edit, then confirm
            </Button>
            <Button type="button" variant="ghost" size="sm" disabled={held} aria-label={`Reject proposal ${proposal.name}`}
              onClick={() => setMode("reject")}>
              Reject
            </Button>
          </>
        ) : mode === "edit" ? (
          <>
            <Button type="button" size="sm" disabled={held || Object.keys(changed).length === 0}
              onClick={() => void run(() => confirmSkillProposal(projectId, proposal.proposal_id, proposal.content_sha256, changed), true)}>
              {working && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Save and confirm
            </Button>
            <Button type="button" variant="ghost" size="sm" disabled={held} onClick={() => setMode("view")}>Cancel</Button>
          </>
        ) : (
          <>
            <Button type="button" size="sm" disabled={held}
              onClick={() => void run(() => rejectSkillProposal(projectId, proposal.proposal_id, reason.trim()))}>
              {working && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
              Reject proposal
            </Button>
            <Button type="button" variant="ghost" size="sm" disabled={held} onClick={() => setMode("view")}>Cancel</Button>
          </>
        )}
      </div>
    </li>
  );
}

/**
 * Skills agents proposed for this project, awaiting the operator. A proposal changes nothing until Confirm, which
 * names the hash of what is shown. Nothing renders while proposals are off and none exist.
 */
export function SkillProposals({ projectId, onWritten }: { projectId: string; onWritten: () => Promise<unknown> | void }) {
  const id = useId();
  const [list, setList] = useState<SkillProposalList | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setList(await fetchSkillProposals(projectId));
      setError("");
    } catch (err) {
      setError(errorText(err));
    }
  }, [projectId]);

  useEffect(() => {
    setList(null);
    setNotice("");
    void load();
  }, [load]);

  const changed = async () => {
    setBusy(true);
    try {
      await load();
    } finally {
      setBusy(false);
    }
  };
  const conflict = async (message: string) => {
    setNotice(`${message} The list is reloaded.`);
    await load();
  };

  if (error && !list) return <p role="alert" className="break-words text-xs text-destructive">{error}</p>;
  if (!list || (list.mode !== "on" && list.proposals.length === 0)) return null;
  return (
    <div className="space-y-2" aria-labelledby={`${id}-title`} role="group">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h3 id={`${id}-title`} className="text-xs font-medium text-muted-foreground">Proposed skills</h3>
        <span className="text-[11px] tabular-nums text-muted-foreground">{list.proposals.length} waiting</span>
      </div>
      <p className="text-xs text-muted-foreground">
        Agents draft these. Nothing here has taken effect: no run sees a proposal until you confirm it.
        {list.mode !== "on" && " Proposals are switched off for this project; the ones below were made before."}
      </p>
      {notice && <p role="alert" className="break-words text-xs text-destructive">{notice}</p>}
      {error && <p role="alert" className="break-words text-xs text-destructive">{error}</p>}
      {list.proposals.length === 0 ? (
        <p className="text-xs text-muted-foreground">No proposals are waiting.</p>
      ) : (
        <ul className="rounded-md border border-border px-3">
          {list.proposals.map((proposal) => (
            <ProposalCard
              key={`${proposal.proposal_id}:${proposal.content_sha256}:${proposal.status}`}
              projectId={projectId}
              proposal={proposal}
              busy={busy}
              onChanged={changed}
              onConflict={conflict}
              onWritten={onWritten}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

/** The project's switch for agent proposals. It saves at once, through its own operator-only operation. */
export function SkillProposalsSetting({ projectId, initial }: { projectId: string; initial: "on" | undefined }) {
  const id = useId();
  const [on, setOn] = useState(initial === "on");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setOn(initial === "on");
    setError("");
  }, [projectId, initial]);

  const change = async (next: boolean) => {
    setBusy(true);
    setError("");
    try {
      setOn((await setSkillProposalMode(projectId, next ? "on" : "off")) === "on");
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby={`${id}-title`} className="space-y-2 rounded-lg border border-border bg-card px-5 py-4">
      <div className="flex items-center justify-between gap-3">
        <h2 id={`${id}-title`} className="text-sm font-semibold">Skill proposals</h2>
        <Switch label="Allow agents to propose skills" checked={on} disabled={busy} onChange={(next) => void change(next)} />
      </div>
      <p className="text-xs text-muted-foreground">
        Lets agents draft skills for this project. A proposal never takes effect until you confirm it on the
        Skills tab, after reading its full text or its difference from the current version. Turning this off keeps
        the proposals already made and stops new ones.
      </p>
      {error && <p role="alert" className="break-words text-xs text-destructive">{error}</p>}
    </section>
  );
}
