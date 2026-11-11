// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkflowField, WorkflowsSection } from "@/components/workflows";
import { deleteWorkflow, fetchWorkflows, previewWorkflow, putWorkflow, type Workflow } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchWorkflows: vi.fn(), previewWorkflow: vi.fn(), putWorkflow: vi.fn(), deleteWorkflow: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let container: HTMLDivElement, root: Root;
beforeEach(() => {
  vi.mocked(fetchWorkflows).mockResolvedValue([{ key: "dev", name: "Development", description: "", profiles: {}, roles: [], work_role: null, review_panel: null, max_step_retries: null, verification: null, updated_at: "today" } as Workflow]);
  container = document.createElement("div"); document.body.append(container); root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.clearAllMocks(); });
const click = async (label: string) => {
  await act(async () => [...container.querySelectorAll("button")].find((button) => button.textContent === label)!.click());
};
it("shows the server validation error without changing it", async () => {
  vi.mocked(putWorkflow).mockRejectedValue(new Error("roles[0].profile_id cannot run work"));
  await act(async () => root.render(<WorkflowsSection />));
  await click("Edit");
  expect(container.querySelector<HTMLTextAreaElement>("textarea")!.value).not.toContain("updated_at");
  await click("Save");
  expect(container.querySelector('[role="alert"]')?.textContent).toBe("roles[0].profile_id cannot run work");
  expect(container.querySelector("textarea")).not.toBeNull();
});
it("requires a second click to delete and preserves the server confirmation", async () => {
  const pending = { status: "needs_confirmation" as const, confirmation_id: "confirm-delete", expires_in_seconds: 60, session: "session", impact: "Clear the project default?" };
  vi.mocked(deleteWorkflow).mockResolvedValueOnce(pending).mockResolvedValueOnce(undefined);
  await act(async () => root.render(<WorkflowsSection />));
  await click("Delete");
  expect(deleteWorkflow).not.toHaveBeenCalled();
  await click("Cancel");
  expect(deleteWorkflow).not.toHaveBeenCalled();
  await click("Delete");
  await click("Delete workflow");
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(pending.impact);
  await click("Delete workflow");
  expect(deleteWorkflow).toHaveBeenLastCalledWith("dev", pending);
  expect(fetchWorkflows).toHaveBeenCalledTimes(2);
});
it("discards a stale task preview and shows a preview failure without default role claims", async () => {
  let complete!: (lines: string[]) => void;
  vi.mocked(previewWorkflow).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }))
    .mockRejectedValueOnce(new Error("Profile unavailable"));
  const rows = [{ key: "dev", name: "Development", profiles: { worker: "fixed" } },
    { key: "check", name: "Check", profiles: {} }] as Workflow[];
  const render = (value: string) => root.render(<WorkflowField rows={rows} value={value} onChange={() => {}}
    preview={{ projectId: "demo", profiles: { worker: "other", planner: "chosen", reviewer: "" } }} />);
  await act(async () => render("dev"));
  expect(previewWorkflow).toHaveBeenLastCalledWith("dev", "demo", { planner: "chosen" });
  await act(async () => render("check"));
  expect(previewWorkflow).toHaveBeenLastCalledWith("check", "demo", { worker: "other", planner: "chosen" });
  await act(async () => complete(["Stale configuration"]));
  expect(container.textContent).not.toContain("Stale configuration");
  expect(container.querySelector('[role="alert"]')?.textContent).toBe("Profile unavailable");
  expect(container.textContent).not.toContain("default worker");
  await act(async () => render("none"));
  expect(container.querySelector('[role="alert"]')).toBeNull();
});
