// @vitest-environment happy-dom
import { act, useState } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { executionChanges, executionDraft, ProjectExecutionFields } from "@/components/project-execution";
import type { Project } from "@/lib/api";
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), fetchWorkflows: vi.fn(async () => []) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const project = {} as Project;
it("keeps untouched null sub-agent settings out of updates", () => {
  expect(executionChanges(executionDraft(project), project)).toEqual({});
  expect(executionChanges({ ...executionDraft(project), subagents: "on", subagent_budget_usd: "0.25", subagents_per_run: "0" }, project))
    .toEqual({ subagents: "on", subagent_budget_usd: 0.25, subagents_per_run: 0 });
});
describe("Sub-agents project settings", () => {
  it("explains spawn tools and cost, changes its switch, and displays limits and invalid ranges", async () => {
    const host = document.createElement("div"); const root = createRoot(host);
    function Form() { const [draft, setDraft] = useState(executionDraft(project)); return <ProjectExecutionFields idPrefix="test" draft={draft} onChange={setDraft} />; }
    try {
      await act(async () => root.render(<Form />));
      const toggle = host.querySelector('[role="switch"][aria-label="Sub-agents"]') as HTMLButtonElement;
      expect(toggle.getAttribute("aria-checked")).toBe("false");
      expect(host.textContent).toContain("Planner and Workers get spawn tools");
      expect(host.textContent).toContain("read-only"); expect(host.textContent).toContain("cost money");
      await act(async () => toggle.click());
      expect(toggle.getAttribute("aria-checked")).toBe("true");
      for (const field of ["subagent_budget_usd", "subagent_task_budget_usd", "subagents_per_run", "subagents_per_task"])
        expect(host.querySelector(`#test-${field}`)?.getAttribute("placeholder")).toBeTruthy();
      const input = host.querySelector("#test-subagents_per_run") as HTMLInputElement;
      await act(async () => {
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "21");
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      expect(host.querySelector('[role="alert"]')?.textContent).toContain("0–20");
      expect(input.getAttribute("aria-invalid")).toBe("true");
    } finally { act(() => root.unmount()); }
  });
});

it("offers an explicit ask-at-cap action while retaining fail as the inherited action", async () => {
  const host = document.createElement("div"); const root = createRoot(host);
  try {
    await act(async () => root.render(<ProjectExecutionFields idPrefix="budget" draft={executionDraft(project)} onChange={() => {}} />));
    const select = host.querySelector("#budget-task_budget_action") as HTMLSelectElement;
    expect(select.value).toBe("");
    expect(select.querySelector('option[value="ask"]')?.textContent).toContain("enforce");
    expect(executionChanges({ ...executionDraft(project), task_budget_action: "ask" }, project))
      .toEqual({ task_budget_action: "ask" });
  } finally { act(() => root.unmount()); }
});
