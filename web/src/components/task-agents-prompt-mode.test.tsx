// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskAgentsSection } from "@/components/task-agents";
import type { SystemPromptMode, TaskAgents } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const profile = (runtime: string) => ({
  value: { profile_id: "p", name: "p", model: "m", effort: null, runtime, provider: "v" },
  source: "role" as const,
});
const mode = (value: SystemPromptMode, source: "task" | "role" | "builtin") => ({ value, source });
const limit = { value: 2, source: "role" as const, task: null, default: { value: 2, source: "role" as const } };

function agents(editable = true): TaskAgents {
  return {
    concurrency_max: 64, editable,
    roles: {
      planner: { profile: profile("Claude Code"), runtime_type: "claude-code", system_prompt_mode: mode("replace", "builtin") },
      worker: { profile: profile("Codex"), runtime_type: "codex", system_prompt_mode: mode("append", "builtin"), concurrency: limit },
      reviewer: { profile: profile("Claude Code"), runtime_type: "claude-code", system_prompt_mode: mode("append", "task"), concurrency: limit },
    },
  };
}

describe("a task's system prompt mode", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("is shown for every role, on any runtime, saves a choice and resets the task's own", async () => {
    const bodies: unknown[] = [];
    const savedAgents = agents();
    const onSaved = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input === "/api/v1/tasks/agents" && init?.method === "POST") {
        bodies.push(JSON.parse(String(init.body)));
        return Response.json({ status: "ok", result: { task_id: "task-1", agents: savedAgents } });
      }
      if (input === "/api/v1/registry/view") {
        return Response.json({ status: "ok", result: { profiles: [], runtimes: [], providers: [], runtime_types: [], limits: {} } });
      }
      throw new Error(`Unexpected request: ${init?.method ?? "GET"} ${input}`);
    }));
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<TaskAgentsSection taskId="task-1" agents={agents()} onSaved={onSaved} />));
    const fields = [...container.querySelectorAll<HTMLSelectElement>("select[id$='-mode']")];
    expect(fields).toHaveLength(3); // planner, worker (on Codex) and reviewer
    expect(fields.map((f) => f.value)).toEqual(["replace", "append", "append"]);
    await act(async () => {
      fields[0].value = "append";
      fields[0].dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(bodies).toEqual([{ task_id: "task-1", system_prompt_modes: { planner: "append" } }]);
    expect(onSaved).toHaveBeenCalledExactlyOnceWith(savedAgents);
    const reset = [...container.querySelectorAll("button")].filter((b) => b.textContent === "task setting · reset");
    expect(reset).toHaveLength(1); // only the reviewer's mode is the task's own
    await act(async () => reset[0].click());
    expect(bodies).toEqual([
      { task_id: "task-1", system_prompt_modes: { planner: "append" } },
      { task_id: "task-1", system_prompt_modes: { reviewer: null } },
    ]);
    expect(onSaved.mock.calls).toEqual([[savedAgents], [savedAgents]]);
    act(() => root.unmount());
    container.remove();
  });

  it("is text only once the task has completed", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok", result: { profiles: [], runtimes: [], providers: [], runtime_types: [], limits: {} } }));
    vi.stubGlobal("fetch", fetcher);
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<TaskAgentsSection taskId="task-1" agents={agents(false)} onSaved={() => {}} />));
    expect(container.querySelector("select[id$='-mode']")).toBeNull();
    expect(container.textContent).toContain("Replace the runtime's prompt");
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/registry/view", expect.objectContaining({ method: "POST", body: "{}" }));
    act(() => root.unmount());
    container.remove();
  });
});
