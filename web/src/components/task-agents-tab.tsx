import { useEffect, useRef, useState } from "react";
import { Bot, ChevronRight, Loader2 } from "lucide-react";
import { TaskAgentsSection } from "@/components/task-agents";
import { Badge } from "@/components/ui/badge";
import {
  fetchLlmRecords,
  runProfile,
  type LlmRecord,
  type PlannerRole,
  type TaskAgents,
  type TaskDetail,
  type TaskLimitRole,
  type TaskRole,
  type TaskStep,
} from "@/lib/api";
import { roleLabel } from "@/lib/runtime-catalog";
import { AGENT_PROFILE } from "@/lib/settings-pages";
import { profileName } from "@/lib/use-registry";
import { cn } from "@/lib/utils";
import { historyHref, sourceName } from "@/pages/agent-history";

const ROLES: TaskRole[] = ["planner", "worker", "reviewer"];
// The most runs of one role `GET /api/llm-records` answers with.
const RUN_LIMIT = 500;

/** The task's Agent Profile per role on one line, opening the Agents tab. */
export function TaskAgentsSummary({ agents, onOpen }: { agents: TaskAgents; onOpen: () => void }) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex min-h-11 w-full flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-border bg-card px-4 py-2 text-left text-xs transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring motion-reduce:transition-none"
    >
      <Bot className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      {ROLES.map((role) => (
        <span key={role}>
          <span className="text-muted-foreground">{roleLabel(role)}: </span>
          <span className="font-mono text-foreground">{profileName(agents.roles[role].profile.value)}</span>
        </span>
      ))}
      <span className="ml-auto flex items-center gap-0.5 font-medium text-primary">
        Agents
        <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
      </span>
    </button>
  );
}

/** A Planner-defined role's Agent Profile: its own, else its kind's built-in role's; one the project no longer allows
 * is marked, as it runs with the built-in role's. */
export function RoleProfile({ role }: { role: PlannerRole }) {
  const builtin = role.kind === "work" ? "Worker" : "Reviewer";
  return (
    <>
      <span className="text-xs">
        <span className="text-muted-foreground">Profile</span>{" "}
        {role.profile ? role.profile.name : `as the built-in ${builtin}`}
      </span>
      {role.profile && !role.profile.allowed && (
        <Badge variant="warning" title={`The project no longer allows this profile: it runs as the built-in ${builtin}'s`}>
          not allowed now
        </Badge>
      )}
    </>
  );
}

/** One run of one of the task's steps: the step, what it ran as, the Agent Profile it ran on and where that came
 * from. */
export interface StepRun {
  id: number;
  agentId: string;
  /** null: a step that is not in the task's plan now (a start over replaced it), or a run that recorded none. */
  step: TaskStep | null;
  /** "Worker" or "Reviewer", or the Planner-defined role it ran as ("tester · work"). */
  role: string;
  /** It ran as a role the Planner defined (named in mono). */
  definedRole: boolean;
  /** null: a run recorded before Agent Profiles. */
  profile: string | null;
  source: string | null;
  status: LlmRecord["status"];
}

/** The task's Worker and Reviewer runs (each role's records as `GET /api/llm-records` answers for it) by step (in
 * plan order), then by when each started. */
export function stepRuns(steps: TaskStep[], records: Record<TaskLimitRole, LlmRecord[]>): StepRun[] {
  const byId = new Map(steps.map((step) => [step.id, step]));
  const stepOf = (record: LlmRecord) => byId.get(record.metadata?.step_id ?? "") ?? null;
  const order = (record: LlmRecord) => stepOf(record)?.step_index ?? Number.MAX_SAFE_INTEGER;
  const runs = (["worker", "reviewer"] as const).flatMap((role) => records[role].map((record) => ({ role, record })));
  return runs
    .sort(({ record: a }, { record: b }) => order(a) - order(b) || a.created_at.localeCompare(b.created_at) || a.id - b.id)
    .map(({ role, record }) => {
      const profile = runProfile(record);
      const defined = record.metadata?.prompt_layers?.task_role;
      return {
        id: record.id,
        agentId: record.agent_id,
        step: stepOf(record),
        role: defined ? `${defined.id} · ${defined.kind}` : roleLabel(role),
        definedRole: !!defined,
        profile: profile && profileName({ name: profile.name, effort: record.metadata?.model_choice?.effort ?? null }),
        source: profile && sourceName(profile.source),
        status: record.status,
      };
    });
}

const STATUS_VARIANTS: Record<LlmRecord["status"], "success" | "destructive" | "warning"> = {
  completed: "success",
  failed: "destructive",
  running: "warning",
  interrupted: "warning",
};

const CELL = "px-4 py-2 align-top";

export function StepRunsTable({ runs, taskId }: { runs: StepRun[]; taskId: string }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card">
      <table className="w-full whitespace-nowrap text-left text-xs">
        <thead className="text-muted-foreground">
          <tr>
            {["Step", "Role", AGENT_PROFILE.name, "Source", "Status", "Run"].map((name) => (
              <th key={name} scope="col" className="px-4 py-2 font-medium">
                {name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border border-t border-border">
          {runs.map((run) => (
            <tr key={run.id}>
              <td className={CELL} title={run.step?.instruction}>
                {run.step ? (
                  <>
                    <span className="text-muted-foreground">#{run.step.step_index + 1}</span>{" "}
                    <span className="font-mono">{run.step.step_key}</span>
                  </>
                ) : (
                  <span className="text-muted-foreground">not in the plan now</span>
                )}
              </td>
              <td className={cn(CELL, run.definedRole && "font-mono")}>{run.role}</td>
              <td className={cn(CELL, "font-mono")}>{run.profile ?? <span className="text-muted-foreground">not recorded</span>}</td>
              <td className={CELL}>{run.source ?? "–"}</td>
              <td className={CELL}>
                <Badge variant={STATUS_VARIANTS[run.status]}>{run.status}</Badge>
              </td>
              <td className={CELL}>
                <a
                  href={historyHref(run.agentId, taskId)}
                  title="Open this run's history"
                  className="font-mono text-muted-foreground underline decoration-dotted underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                >
                  {run.agentId}
                </a>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** What each of the task's step runs ran on, read again whenever a step changes. */
function StepRuns({ task }: { task: TaskDetail }) {
  const [records, setRecords] = useState<Record<TaskLimitRole, LlmRecord[]> | null>(null);
  const [error, setError] = useState("");
  const latest = useRef(0);
  const steps = task.steps || [];
  // A new run or a run's end moves its step, so the records are read again when a step changes.
  const changes = [
    task.status,
    ...steps.map((s) => [s.id, s.status, s.worker_id, s.reviewer_id, s.review_stage, s.retries].join(":")),
  ].join("|");

  useEffect(() => {
    const seq = ++latest.current;
    Promise.all([
      fetchLlmRecords({ task_id: task.task_id, role: "worker", limit: RUN_LIMIT }),
      fetchLlmRecords({ task_id: task.task_id, role: "reviewer", limit: RUN_LIMIT }),
    ]).then(
      ([worker, reviewer]) => {
        if (seq !== latest.current) return;
        setRecords({ worker, reviewer });
        setError("");
      },
      (err) => {
        if (seq === latest.current) setError(err instanceof Error ? err.message : String(err));
      },
    );
  }, [task.task_id, changes]);

  const clipped = records
    ? (["worker", "reviewer"] as const).filter((role) => records[role].length >= RUN_LIMIT).map(roleLabel)
    : [];
  const runs = records ? stepRuns(steps, records) : [];
  return (
    <section aria-labelledby="task-step-runs-title">
      <h3 id="task-step-runs-title" className="mb-1 text-sm font-semibold">
        What each step ran on
      </h3>
      <p className="mb-2 text-xs text-muted-foreground">
        Every Worker and Reviewer run of the task's steps, with the {AGENT_PROFILE.name} it ran on and where that came
        from. A run keeps the profile it started with.
      </p>
      {error && (
        <p role="alert" className="text-xs text-destructive">
          Could not read the runs: {error}
        </p>
      )}
      {!records && !error && (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          Reading the runs…
        </p>
      )}
      {records && runs.length === 0 && <p className="text-xs text-muted-foreground">No step has run yet.</p>}
      {runs.length > 0 && <StepRunsTable runs={runs} taskId={task.task_id} />}
      {clipped.length > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          Only the latest {RUN_LIMIT} {clipped.join(" and ")} runs are listed.
        </p>
      )}
    </section>
  );
}

/** The roles the task's Planner defined, each with its kind and Agent Profile; their prompts and numbers are under
 * Overview → Roles. */
function DefinedRoles({ roles }: { roles: PlannerRole[] }) {
  return (
    <section aria-labelledby="task-defined-roles-title">
      <h3 id="task-defined-roles-title" className="mb-1 text-sm font-semibold">
        Roles the Planner defined
      </h3>
      <p className="mb-2 text-xs text-muted-foreground">
        A step that names one runs as it, on its {AGENT_PROFILE.name}; their prompts, versions and numbers are under
        Overview → Roles.
      </p>
      <ul className="divide-y divide-border rounded-lg border border-border bg-card">
        {roles.map((role) => (
          <li key={role.id} className="flex flex-wrap items-center gap-x-2 gap-y-1 px-4 py-2.5">
            <span className="break-all font-mono text-sm font-semibold">{role.id}</span>
            <Badge variant="outline">{role.kind === "work" ? "Work" : "Review"}</Badge>
            <RoleProfile role={role} />
          </li>
        ))}
      </ul>
    </section>
  );
}

/** Which agents the task runs on: each role's Agent Profile and limits (changed here until the task completes), the
 * roles its Planner defined, and what each step run ran on. */
export function TaskAgentsTab({ task, onAgents }: { task: TaskDetail; onAgents: (agents: TaskAgents) => void }) {
  return (
    <div className="space-y-6 p-5">
      {task.agents && <TaskAgentsSection taskId={task.task_id} agents={task.agents} onSaved={onAgents} />}
      {task.roles?.length ? <DefinedRoles roles={task.roles} /> : null}
      <StepRuns task={task} />
    </div>
  );
}
