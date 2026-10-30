// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectSettingsForm } from "@/components/project-settings";
import { PROJECTS_CHANGED_EVENT, type Project } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const project = (counts: Partial<Project> = {}): Project => ({
  project_id: "project-example", name: "Example", work_dir: "", description: "", background: "",
  task_count: 0, active_count: 0, schedule_count: 0, ...counts,
} as Project);

describe("project deletion", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    resetSessionGuard();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    resetSessionGuard();
    vi.unstubAllGlobals();
  });
  const button = (text: string) => [...container.querySelectorAll("button")].find((item) => item.textContent === text)!;
  const section = () => container.querySelector("section")!;

  it.each([
    { counts: {}, hint: "Removes the project with its knowledge and skills.", confirmation: "Delete Example?" },
    { counts: { task_count: 4, active_count: 1, schedule_count: 2 }, hint: "Removes the project with its knowledge and skills, its 4 tasks (the 1 running ones are stopped first; result branches stay), and its 2 schedules with their run history.", confirmation: "Delete Example and its 4 tasks?" },
    { counts: { task_count: 3 }, hint: "Removes the project with its knowledge and skills, its 3 tasks (result branches stay).", confirmation: "Delete Example and its 3 tasks?" },
    { counts: { schedule_count: 2 }, hint: "Removes the project with its knowledge and skills, and its 2 schedules with their run history.", confirmation: "Delete Example?" },
  ])("offers deletion and only nonzero impact clauses: $counts", async ({ counts, hint, confirmation }) => {
    await act(async () => root.render(<ProjectSettingsForm project={project(counts)} onDeleted={() => {}} />));
    expect(section().querySelector("p")?.textContent).toBe(hint);
    expect(button("Delete project")).toBeDefined();
    await act(async () => button("Delete project").click());
    expect(section().textContent).toContain(confirmation);
  });

  it("confirms through deleteProject, shows its pending state, then refreshes selectors and navigates", async () => {
    let complete!: (response: Response) => void;
    const fetcher = vi.fn(() => new Promise<Response>((resolve) => { complete = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    const changed = vi.fn();
    const onDeleted = vi.fn();
    window.addEventListener(PROJECTS_CHANGED_EVENT, changed);
    try {
      await act(async () => root.render(<ProjectSettingsForm project={project({ task_count: 4, active_count: 1 })} onDeleted={onDeleted} />));
      await act(async () => button("Delete project").click());
      expect(fetcher).not.toHaveBeenCalled();
      await act(async () => button("Delete").click());
      expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/project_admin/delete", expect.objectContaining({
        method: "POST", body: JSON.stringify({ project_id: "project-example" }),
      }));
      expect(button("Stopping and deleting…").disabled).toBe(true);
      expect(button("Stopping and deleting…").querySelector(".animate-spin")).not.toBeNull();
      expect(onDeleted).not.toHaveBeenCalled();
      await act(async () => complete(Response.json({ status: "ok", result: { deleted: "project-example" } })));
      expect(changed).toHaveBeenCalledOnce();
      expect(onDeleted).toHaveBeenCalledOnce();
    } finally { window.removeEventListener(PROJECTS_CHANGED_EVENT, changed); }
  });

  it("shows a 409's reason, keeps deletion available, and lets the operator finish on retry", async () => {
    const message = "1 task(s) of project 'Example' are still stopping (task-a): delete the project again to finish";
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ status: "error", error: {
      code: "conflict", message,
    } }, { status: 409 })).mockResolvedValueOnce(Response.json({ status: "ok", result: { deleted: "project-example" } }));
    vi.stubGlobal("fetch", fetcher);
    const onDeleted = vi.fn();
    await act(async () => root.render(<ProjectSettingsForm project={project({ task_count: 4, active_count: 1, schedule_count: 2 })} onDeleted={onDeleted} />));
    await act(async () => button("Delete project").click());
    await act(async () => button("Delete").click());
    expect(container.querySelector("[role=alert]")?.textContent).toBe(message);
    expect(section().textContent).toContain("Delete Example and its 4 tasks?");
    expect(button("Delete").disabled).toBe(false);
    expect(onDeleted).not.toHaveBeenCalled();
    await act(async () => button("Delete").click());
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(onDeleted).toHaveBeenCalledOnce();
  });
});
