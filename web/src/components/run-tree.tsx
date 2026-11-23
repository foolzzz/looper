import { useEffect, useState } from "react";
import { Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { fetchRunTree, type RunTreeCost, type RunTreeNode } from "@/lib/api";
import { formatSeconds, formatWhen } from "@/lib/node-cards";
import { formatUsd } from "@/lib/usage";

const STATES: Record<string, string> = {
  queued: "Queued", asking: "Running", finishing: "Finishing", cancelling: "Cancelling", cancelled: "Cancelled",
  answered: "Answered", failed: "Failed", over_budget: "Over budget", running: "Running", completed: "Completed",
  interrupted: "Interrupted", drafting: "Drafting", linting: "Checking request", needs_info: "Needs information", blocked: "Blocked",
};
const KINDS: Record<string, string> = { planner: "Planner", worker: "Worker", reviewer: "Reviewer", advisor: "Advisor",
  advice_check: "Advice check", consult: "Room consult", explorer: "Explorer", standard: "Run" };
const REASONS: Record<string, string> = { parent_ended: "the parent run ended", task_ended: "the task ended",
  step_dropped: "the step was dropped", requested: "requested by the spawner", operator: "cancelled by the operator", server: "the server stopped" };
const time = (value: string) => new Date(value).getTime();
const order = (a: RunTreeNode, b: RunTreeNode) => a.started_at.localeCompare(b.started_at) || a.id - b.id;
const title = (node: RunTreeNode) => `${KINDS[node.kind] ?? node.kind} · Run ${node.id}`;

function Cost({ value }: { value: RunTreeCost }) {
  return <span>{value.subscription_usd == null ? "cost unknown" : formatUsd(value.subscription_usd)} subscription equivalent · {value.api_usd == null ? "cost unknown" : formatUsd(value.api_usd)} API</span>;
}
function NodeDetails({ node }: { node: RunTreeNode }) {
  const state = node.state ?? node.status;
  return <article className="min-w-0 rounded-md border border-border bg-card px-3 py-3">
    <div className="flex flex-wrap items-center gap-2">
      <h4 className="text-sm font-medium">{title(node)}</h4>
      <Badge variant={state === "failed" || state === "over_budget" ? "destructive" : state === "answered" || state === "completed" ? "success" : "outline"}>{STATES[state] ?? state}</Badge>
      <span className="text-xs text-muted-foreground">{formatSeconds(((node.ended_at ? time(node.ended_at) : Date.now()) - time(node.started_at)) / 1000)}{!node.ended_at && " so far"}</span>
    </div>
    <p className="mt-1 break-words text-xs text-muted-foreground">Created by {node.spawner.seat ?? node.spawner.role ?? "task"} · {node.reason}</p>
    <p className="mt-1 break-all font-mono text-xs">{node.model ?? "Model not recorded"}</p>
    <dl className="mt-2 space-y-1 text-xs text-muted-foreground">
      <div><dt className="inline font-medium">Own: </dt><dd className="inline"><Cost value={node.own_cost} /></dd></div>
      <div><dt className="inline font-medium">Subtree: </dt><dd className="inline"><Cost value={node.subtree_cost} /></dd></div>
    </dl>
    {node.result_summary && <p className="mt-2 whitespace-pre-wrap break-words text-xs">{node.result_verified === false && <Badge variant="warning" className="mr-1">Unverified</Badge>}{node.result_summary}</p>}
    {node.cancel_reason && <p className="mt-2 text-xs text-muted-foreground">Cancelled: {REASONS[node.cancel_reason] ?? node.cancel_reason}</p>}
    {(node.failure_class || node.failure_cause) && <p className="mt-2 text-xs text-destructive">Failure: {[node.failure_class, node.failure_cause].filter(Boolean).join(" · ")}</p>}
    {node.parent_spend_known === false && <p className="mt-2 text-xs text-warning">Parent spend was unknown at admission</p>}
  </article>;
}

/** One task's persisted runs: the hierarchy keeps record parents; narrow screens replace bars with a time list. */
export function RunTreeContent({ nodes }: { nodes: RunTreeNode[] }) {
  if (!nodes.length) return <p className="p-5 text-sm text-muted-foreground">No runs yet</p>;
  const sorted = [...nodes].sort(order);
  const ids = new Set(nodes.map((node) => node.id));
  // A malformed historical cycle must not hide runs or recurse forever.
  const seen = new Set<number>();
  const branch = (node: RunTreeNode): React.ReactNode => {
    if (seen.has(node.id)) return null;
    seen.add(node.id);
    const children = sorted.filter((child) => child.parent_record_id === node.id && !seen.has(child.id));
    return <li key={node.id} data-run-id={node.id} className="min-w-0 space-y-2">
      <NodeDetails node={node} />
      {children.length > 0 && <ul className="ml-2 space-y-2 border-l border-border pl-3 sm:ml-4">{children.map(branch)}</ul>}
    </li>;
  };
  const roots = sorted.filter((node) => node.parent_record_id == null || !ids.has(node.parent_record_id));
  const tree = roots.map(branch);
  tree.push(...sorted.filter((node) => !seen.has(node.id)).map(branch));
  const start = Math.min(...sorted.map((node) => time(node.started_at)));
  const end = Math.max(start + 1, ...sorted.map((node) => node.ended_at ? time(node.ended_at) : Date.now()));
  const percent = (at: number) => `${Math.max(0, Math.min(100, (at - start) / (end - start) * 100))}%`;
  return <div className="space-y-6 p-5">
    <section aria-label="Run timeline" className="hidden sm:block">
      <h3 className="text-sm font-semibold">Timeline</h3>
      <p className="mb-3 mt-1 text-xs text-muted-foreground">{formatWhen(sorted[0].started_at)} · {formatSeconds((end - start) / 1000)} · Dashed segments are waits for sub-agents.</p>
      <ul className="space-y-2">
        {sorted.map((node) => <li key={node.id} className="grid grid-cols-[10rem_1fr] items-center gap-3 text-xs">
          <span>{title(node)}</span>
          <div className="relative h-6 rounded bg-muted">
            <span data-run-bar={node.id} role="img" aria-label={`${title(node)}: ${STATES[node.state ?? node.status] ?? node.status}`} className="absolute inset-y-1 min-w-0.5 rounded bg-primary/60"
              style={{ left: percent(time(node.started_at)), width: percent((node.ended_at ? time(node.ended_at) : end) - time(node.started_at) + start) }} />
            {node.wait_intervals.map((wait, i) => <span key={i} role="img" aria-label="Waiting for sub-agents" className="absolute inset-y-1 min-w-0.5 rounded border-2 border-dashed border-foreground bg-card"
              title={`${formatWhen(wait.started_at)} – ${wait.ended_at ? formatWhen(wait.ended_at) : "still waiting"}`}
              style={{ left: percent(time(wait.started_at)), width: percent((wait.ended_at ? time(wait.ended_at) : end) - time(wait.started_at) + start) }} />)}
          </div>
        </li>)}
      </ul>
    </section>
    <ol aria-label="Runs in time order" className="space-y-2 sm:hidden">
      {sorted.map((node) => <li key={node.id} data-run-id={node.id} className="text-xs text-muted-foreground">{formatWhen(node.started_at)} · {title(node)} · {STATES[node.state ?? node.status] ?? node.status}{node.wait_intervals.length > 0 && ` · ${node.wait_intervals.length} wait${node.wait_intervals.length === 1 ? "" : "s"}`}</li>)}
    </ol>
    <section aria-label="Run hierarchy" className="min-w-0">
      <h3 className="mb-3 text-sm font-semibold">Run tree</h3>
      <ul className="space-y-3">{tree}</ul>
    </section>
  </div>;
}

export function RunTree({ taskId, refreshKey = "" }: { taskId: string; refreshKey?: string }) {
  const [data, setData] = useState<{ taskId: string; nodes: RunTreeNode[] } | null>(null);
  const nodes = data?.taskId === taskId ? data.nodes : null;
  const [error, setError] = useState("");
  useEffect(() => {
    const abort = new AbortController();
    setError("");
    fetchRunTree(taskId, abort.signal).then((result) => { if (!abort.signal.aborted) setData({ taskId, nodes: result.nodes }); },
      (err) => { if (!abort.signal.aborted) setError(err instanceof Error ? err.message : String(err)); });
    return () => abort.abort();
  }, [taskId, refreshKey]);
  return <>
    {error && <p role="alert" className="p-5 text-sm text-destructive">Could not read the run tree: {error}</p>}
    {nodes === null && !error && <p role="status" className="flex items-center gap-2 p-5 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />Loading run tree…</p>}
    {nodes !== null && <RunTreeContent nodes={nodes} />}
  </>;
}
