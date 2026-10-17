import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { StepRunsTable, TaskAgentsSummary, TaskAgentsTab, stepRuns } from "@/components/task-agents-tab";
import type { LlmRecord, RunSettingSource, TaskAgents, TaskDetail, TaskStep } from "@/lib/api";

const profile = (name: string, effort: string | null = null) => ({
  value: { profile_id: name, name, model: "m", effort, runtime: "r", provider: "p" },
  source: "role" as const,
});
const limit = (value: number) => ({ value, source: "role_project" as const, task: null, project: { value, source: "role_project" as const } });

function agents(editable: boolean): TaskAgents {
  return {
    concurrency_max: 64,
    editable,
    roles: {
      planner: { profile: profile("claude-code/opus"), concurrency: { value: 3, source: "role_project" } },
      worker: { profile: profile("codex/gpt-5", "high"), concurrency: limit(2) },
      reviewer: { profile: profile("claude-code/sonnet"), concurrency: limit(1) },
    },
  };
}

const step = (id: string, step_index: number) => ({ id, step_index, step_key: id, instruction: `do ${id}` }) as TaskStep;
const STEPS = [step("s1", 0), step("s2", 1)];

function record(
  id: number,
  agent_id: string,
  step_id: string,
  source: RunSettingSource,
  extra: { created_at?: string; task_role?: { id: string; kind: "work" | "review"; version: number } } = {},
): LlmRecord {
  return {
    id,
    agent_id,
    status: "completed",
    created_at: extra.created_at ?? `2026-10-16T10:00:0${id}+00:00`,
    metadata: {
      step_id,
      profile: {
        id: "codex/gpt-5",
        name: "codex/gpt-5",
        model: "gpt-5",
        runtime: { id: "codex", name: "codex", type: "codex" },
        provider: { id: "openai", name: "openai", auth: "oauth" },
        source,
      },
      ...(extra.task_role ? { prompt_layers: { task_role: extra.task_role } } : {}),
    },
  } as LlmRecord;
}

const text = (html: string) => html.replace(/<[^>]+>/g, "");

describe("TaskAgentsSummary", () => {
  it("names each role's Agent Profile on one line that opens the Agents tab", () => {
    const html = renderToStaticMarkup(<TaskAgentsSummary agents={agents(true)} onOpen={() => {}} />);
    expect(html.startsWith("<button")).toBe(true);
    for (const line of ["Planner: claude-code/opus", "Worker: codex/gpt-5 · high", "Reviewer: claude-code/sonnet", "Agents"]) {
      expect(text(html)).toContain(line);
    }
  });
});

// Each role's records, as the API answers for it; a run's role is the one it was read as, whatever its id says.
const runsOf = ({ worker = [], reviewer = [] }: { worker?: LlmRecord[]; reviewer?: LlmRecord[] }) => ({ worker, reviewer });

describe("stepRuns", () => {
  it.each([
    ["a Worker", "worker", record(1, "run-a", "s1", "role_project"), { role: "Worker", source: "role in project" }],
    ["a Reviewer", "reviewer", record(2, "run-b", "s1", "project"), { role: "Reviewer", source: "project" }],
    [
      "a run as a role the Planner defined",
      "worker",
      record(3, "run-c", "s2", "task_role", { task_role: { id: "tester", kind: "work", version: 2 } }),
      { role: "tester · work", definedRole: true, source: "role the Planner defined" },
    ],
    [
      "a run on the Planner's choice for its step",
      "reviewer",
      record(4, "run-d", "s2", "planner_step"),
      { role: "Reviewer", source: "Planner's choice for this step" },
    ],
  ] as const)("names %s's role, Agent Profile and source", (_, role, run, expected) => {
    const [row] = stepRuns(STEPS, runsOf({ [role]: [run] }));
    expect(row).toMatchObject({ id: run.id, agentId: run.agent_id, profile: "codex/gpt-5", definedRole: false, ...expected });
    expect(row.step?.id).toBe(run.metadata?.step_id);
  });

  it("lists the runs by step in plan order, then by start, and a step not in the plan now last", () => {
    const runs = stepRuns(STEPS, runsOf({
      worker: [
        record(1, "worker-gone", "s0-replaced", "role"),
        record(3, "worker-s2", "s2", "role", { created_at: "2026-10-16T10:01:00+00:00" }),
        record(4, "worker-s1", "s1", "role"),
      ],
      reviewer: [record(2, "reviewer-s2", "s2", "role", { created_at: "2026-10-16T10:05:00+00:00" })],
    }));
    expect(runs.map((run) => [run.agentId, run.step?.id ?? null])).toEqual([
      ["worker-s1", "s1"],
      ["worker-s2", "s2"],
      ["reviewer-s2", "s2"],
      ["worker-gone", null],
    ]);
  });

  it("links each row to that run's history", () => {
    const html = renderToStaticMarkup(<StepRunsTable runs={stepRuns(STEPS, runsOf({ worker: [record(1, "worker-a", "s1", "role")] }))} taskId="t1" />);
    expect(html).toContain('href="#history/worker-a?task=t1"');
  });
});

describe("TaskAgentsTab", () => {
  const task = (editable: boolean) =>
    ({ task_id: "t1", project_id: "p1", status: editable ? "running" : "completed", steps: STEPS, roles: [], agents: agents(editable) }) as unknown as TaskDetail;

  it("edits the roles' profiles and limits while the task can run", () => {
    const html = renderToStaticMarkup(<TaskAgentsTab task={task(true)} onAgents={() => {}} />);
    expect(html).toContain("<select");
    expect(html).toContain("<input");
  });

  it("shows a completed task's profiles and limits without a control to change them", () => {
    const html = renderToStaticMarkup(<TaskAgentsTab task={task(false)} onAgents={() => {}} />);
    expect(html).not.toContain("<select");
    expect(html).not.toContain("<input");
    expect(html).not.toContain("<button");
    for (const line of ["claude-code/opus", "codex/gpt-5 · high", "claude-code/sonnet", "The task has completed"]) {
      expect(text(html)).toContain(line);
    }
  });

  it("lists the Planner's roles with their profiles, marking one the project no longer allows", () => {
    const role = (id: string, allowed: boolean) => ({ id, kind: "work", profile: { profile_id: id, name: `${id}-profile`, allowed } });
    const html = renderToStaticMarkup(
      <TaskAgentsTab task={{ ...task(true), roles: [role("tester", true), role("fe_rd", false)] } as TaskDetail} onAgents={() => {}} />,
    );
    const rows = text(html).split("Roles the Planner defined")[1];
    expect(rows).toMatch(/tester.*Work.*Profile tester-profile(?!not allowed now).*fe_rd.*Work.*Profile fe_rd-profilenot allowed now/);
  });
});
