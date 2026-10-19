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

  it("is shown for Claude Code roles only, saves a choice and resets the task's own", async () => {
    const bodies: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === "PUT") {
        bodies.push(JSON.parse(String(init.body)));
        return new Response(JSON.stringify({ agents: agents() }), { status: 200 });
      }
      return new Response(JSON.stringify({ profiles: [], runtimes: [], providers: [], runtime_types: [], limits: {} }), { status: 200 });
    }));
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<TaskAgentsSection taskId="task-1" agents={agents()} onSaved={() => {}} />));
    const fields = [...container.querySelectorAll<HTMLSelectElement>("select[id$='-mode']")];
    expect(fields).toHaveLength(2); // planner and reviewer; the worker runs on Codex
    expect(fields.map((f) => f.value)).toEqual(["replace", "append"]);
    await act(async () => {
      fields[0].value = "append";
      fields[0].dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(bodies).toEqual([{ system_prompt_modes: { planner: "append" } }]);
    const reset = [...container.querySelectorAll("button")].filter((b) => b.textContent === "task setting · reset");
    expect(reset).toHaveLength(1); // only the reviewer's mode is the task's own
    await act(async () => reset[0].click());
    expect(bodies[1]).toEqual({ system_prompt_modes: { reviewer: null } });
    act(() => root.unmount());
    container.remove();
  });

  it("is text only once the task has completed", async () => {
    const container = document.createElement("div");
    document.body.append(container);
    const root = createRoot(container);
    await act(async () => root.render(<TaskAgentsSection taskId="task-1" agents={agents(false)} onSaved={() => {}} />));
    expect(container.querySelector("select[id$='-mode']")).toBeNull();
    expect(container.textContent).toContain("Replace Claude Code's prompt");
    act(() => root.unmount());
    container.remove();
  });
});
