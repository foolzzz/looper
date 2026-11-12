// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectSettingsForm } from "@/components/project-settings";
import { PROJECTS_CHANGED_EVENT, fetchWorkflows, previewProjectDelete, type Workflow, type Project } from "@/lib/api";
import { resetSessionGuard } from "@/lib/session";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(), fetchWorkflows: vi.fn(async () => []),
  previewProjectDelete: vi.fn(async () => ({ impact: "Delete Example? This cannot be undone." })),
}));
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
    vi.mocked(previewProjectDelete).mockReset().mockResolvedValue({ impact: "Delete Example? This cannot be undone." });
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "needs_confirmation",
      confirmation_id: "delete-project", expires_in_seconds: 60, impact: "Delete Example?" }, { status: 202 })));
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
  const section = () => container.querySelector('section[aria-labelledby$="-delete"]')!;

  it.each([
    { before: null, choice: "dev", saved: "dev" },
    { before: "dev", choice: "none", saved: null },
  ])("saves the project default workflow ($before → $saved)", async ({ before, choice, saved }) => {
    vi.mocked(fetchWorkflows).mockResolvedValueOnce([{ key: "dev", name: "Development", description: "", profiles: {}, roles: [], work_role: null, review_panel: null, max_step_retries: null, verification: null } as Workflow]);
    const fetcher = vi.fn(async () => Response.json({ status: "ok", result: { project: project({ default_workflow: saved }) } }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<ProjectSettingsForm project={project({ default_workflow: before })} onDeleted={() => {}} />));
    const select = container.querySelector<HTMLSelectElement>("#workflow-choice")!;
    expect(select.value).toBe(before ?? "none");
    await act(async () => { select.value = choice; select.dispatchEvent(new Event("change", { bubbles: true })); });
    await act(async () => container.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/project_admin/update", expect.objectContaining({
      body: JSON.stringify({ project_id: "project-example", fields: { default_workflow: saved } }),
    }));
  });

  it("requires local confirmation before an authenticated operator can delete, and cancels without deleting", async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ status: "ok", result: { deleted: "project-example" } }));
    vi.stubGlobal("fetch", fetcher);
    const onDeleted = vi.fn();
    const changed = vi.fn();
    window.addEventListener(PROJECTS_CHANGED_EVENT, changed);
    try {
      await act(async () => root.render(<ProjectSettingsForm project={project({ task_count: 4, active_count: 1, schedule_count: 2 })} onDeleted={onDeleted} />));
      await act(async () => button("Delete project").click());
      expect(fetcher).not.toHaveBeenCalled();
      expect(onDeleted).not.toHaveBeenCalled();
      expect(changed).not.toHaveBeenCalled();
      expect(section().textContent).toContain("Delete Example? This cannot be undone.");
      expect(section().textContent).toContain("its 4 tasks");
      expect(section().textContent).toContain("its 2 schedules");
      await act(async () => button("Cancel").click());
      expect(button("Delete")).toBeUndefined();
      expect(fetcher).not.toHaveBeenCalled();
      await act(async () => button("Delete project").click());
      expect(fetcher).not.toHaveBeenCalled();
      await act(async () => button("Delete").click());
      expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/project_admin/delete", expect.objectContaining({
        method: "POST", body: JSON.stringify({ project_id: "project-example" }),
      }));
      expect(onDeleted).toHaveBeenCalledOnce();
      expect(changed).toHaveBeenCalledOnce();
    } finally { window.removeEventListener(PROJECTS_CHANGED_EVENT, changed); }
  });

  it("shows scoped Managers before the operator can confirm deletion", async () => {
    const impact = "它在管家「Release」的项目范围里：删除后会从它的范围里去掉。";
    let complete!: (preview: { impact: string }) => void;
    vi.mocked(previewProjectDelete).mockRejectedValueOnce(new Error("Impact unavailable"))
      .mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const onDeleted = vi.fn();
    await act(async () => root.render(<ProjectSettingsForm project={project()} onDeleted={onDeleted} />));
    await act(async () => button("Delete project").click());
    expect(section().querySelector("[role=alert]")?.textContent).toBe("Impact unavailable");
    expect(container.querySelector("form [role=alert]")).toBeNull();
    expect(button("Delete")).toBeUndefined();
    await act(async () => button("Delete project").click());
    expect(button("Checking impact…").disabled).toBe(true);
    expect(section().querySelector("[role=alert]")).toBeNull();
    expect(button("Delete")).toBeUndefined();
    expect(fetcher).not.toHaveBeenCalled();
    await act(async () => complete({ impact }));
    expect(section().textContent).toContain("Release");
    expect(button("Delete").disabled).toBe(false);
    expect(fetcher).not.toHaveBeenCalled();
    expect(onDeleted).not.toHaveBeenCalled();
    await act(async () => button("Cancel").click());
    expect(button("Delete")).toBeUndefined();
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("focuses Cancel when the delete preview resolves", async () => {
    let complete!: (preview: { impact: string }) => void;
    vi.mocked(previewProjectDelete).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; }));
    await act(async () => root.render(<ProjectSettingsForm project={project()} onDeleted={() => {}} />));
    button("Delete project").focus();
    expect(document.activeElement).toBe(button("Delete project"));
    await act(async () => button("Delete project").click());
    expect(button("Cancel")).toBeUndefined();
    await act(async () => complete({ impact: "Delete Example? This cannot be undone." }));
    expect(document.activeElement).toBe(button("Cancel"));
  });

  it("shows the API deletion impact before confirming in the same session", async () => {
    const impact = "它在管家「Review」、「Release」的项目范围里；删除后范围变空的管家看不到也建不了任务。";
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({
      status: "needs_confirmation", confirmation_id: "delete-project", expires_in_seconds: 60, impact,
    }, { status: 202 })).mockResolvedValueOnce(Response.json({ status: "ok", result: { deleted: "project-example" } }));
    vi.stubGlobal("fetch", fetcher);
    const onDeleted = vi.fn();
    await act(async () => root.render(<ProjectSettingsForm project={project()} onDeleted={onDeleted} />));
    await act(async () => button("Delete project").click());
    await act(async () => button("Delete").click());
    expect(section().textContent).toContain(impact);
    expect(onDeleted).not.toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledOnce();
    await act(async () => button("Delete").click());
    expect(fetcher).toHaveBeenLastCalledWith("/api/v1/confirm", expect.objectContaining({
      body: JSON.stringify({ confirmation_id: "delete-project" }),
      headers: expect.objectContaining({ "X-Looper-Session": fetcher.mock.calls[0][1].headers["X-Looper-Session"] }),
    }));
    expect(onDeleted).toHaveBeenCalledOnce();
  });

  it.each([
    { counts: {}, hint: "Removes the project with its knowledge and skills.", confirmation: "Delete Example?" },
    { counts: { task_count: 4, active_count: 1, schedule_count: 2 }, hint: "Removes the project with its knowledge and skills, its 4 tasks (the 1 running ones are stopped first; result branches stay), and its 2 schedules with their run history.", confirmation: "Delete Example and its 4 tasks?" },
    { counts: { task_count: 3 }, hint: "Removes the project with its knowledge and skills, its 3 tasks (result branches stay).", confirmation: "Delete Example and its 3 tasks?" },
    { counts: { schedule_count: 2 }, hint: "Removes the project with its knowledge and skills, and its 2 schedules with their run history.", confirmation: "Delete Example?" },
  ])("offers deletion and only nonzero impact clauses: $counts", async ({ counts, hint, confirmation }) => {
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "needs_confirmation",
      confirmation_id: "delete-project", expires_in_seconds: 60, impact: confirmation }, { status: 202 })));
    await act(async () => root.render(<ProjectSettingsForm project={project(counts)} onDeleted={() => {}} />));
    expect(section().querySelector("p")?.textContent).toBe(hint);
    expect(button("Delete project")).toBeDefined();
    await act(async () => button("Delete project").click());
    await act(async () => button("Delete").click());
    expect(section().textContent).toContain(confirmation);
  });

  it("confirms through deleteProject, shows its pending state, then refreshes selectors and navigates", async () => {
    let complete!: (response: Response) => void;
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ status: "needs_confirmation",
      confirmation_id: "delete-project", expires_in_seconds: 60, impact: "Delete Example and its 4 tasks?" }, { status: 202 }))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { complete = resolve; }));
    vi.stubGlobal("fetch", fetcher);
    const changed = vi.fn();
    const onDeleted = vi.fn();
    window.addEventListener(PROJECTS_CHANGED_EVENT, changed);
    try {
      await act(async () => root.render(<ProjectSettingsForm project={project({ task_count: 4, active_count: 1 })} onDeleted={onDeleted} />));
      await act(async () => button("Delete project").click());
      await act(async () => button("Delete").click());
      expect(fetcher).toHaveBeenCalledOnce();
      expect(fetcher).toHaveBeenCalledWith("/api/v1/project_admin/delete", expect.objectContaining({
        method: "POST", body: JSON.stringify({ project_id: "project-example" }),
      }));
      await act(async () => button("Delete").click());
      expect(fetcher).toHaveBeenLastCalledWith("/api/v1/confirm", expect.objectContaining({
        method: "POST", body: JSON.stringify({ confirmation_id: "delete-project" }),
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
    const pending = () => Response.json({ status: "needs_confirmation", confirmation_id: "delete-project",
      expires_in_seconds: 60, impact: "Delete Example and its 4 tasks?" }, { status: 202 });
    const fetcher = vi.fn().mockResolvedValueOnce(pending()).mockResolvedValueOnce(Response.json({ status: "error", error: {
      code: "conflict", message,
    } }, { status: 409 })).mockResolvedValueOnce(pending())
      .mockResolvedValueOnce(Response.json({ status: "ok", result: { deleted: "project-example" } }));
    vi.stubGlobal("fetch", fetcher);
    const onDeleted = vi.fn();
    await act(async () => root.render(<ProjectSettingsForm project={project({ task_count: 4, active_count: 1, schedule_count: 2 })} onDeleted={onDeleted} />));
    await act(async () => button("Delete project").click());
    await act(async () => button("Delete").click());
    await act(async () => button("Delete").click());
    expect(section().querySelector("[role=alert]")?.textContent).toBe(message);
    expect(container.querySelector("form [role=alert]")).toBeNull();
    expect(button("Delete project").disabled).toBe(false);
    expect(onDeleted).not.toHaveBeenCalled();
    await act(async () => button("Delete project").click());
    await act(async () => button("Delete").click());
    await act(async () => button("Delete").click());
    expect(fetcher).toHaveBeenCalledTimes(4);
    expect(onDeleted).toHaveBeenCalledOnce();
  });

  it.each([
    { name: "..My  Project--V2._-", projectsRoot: "/srv/projects/" },
    { name: "你好...---", projectsRoot: "/srv/projects" },
    { name: "İK_App.v1", projectsRoot: "/" },
  ])("offers a new directory under the configured root for $name", async ({ name, projectsRoot }) => {
    await act(async () => root.render(<ProjectSettingsForm projectsRoot={projectsRoot} project={project({ name, work_dir: "/srv/example" })} onDeleted={() => {}} />));
    const field = container.querySelector('input[placeholder^="Leave empty for"]') as HTMLInputElement;
    expect(field.placeholder).toBe(`Leave empty for a new directory under ${projectsRoot}`);
    expect(button("Preview move").disabled).toBe(false);
  });

  it("explains the directory controls in English and requires a target without a root", async () => {
    await act(async () => root.render(<ProjectSettingsForm project={project({ work_dir: "/srv/example" })} onDeleted={() => {}} />));
    expect(container.textContent).toContain("Moves the directory with all its files and updates the schedules that use it. Anything in progress in the project (a task step, a schedule run or a gate) blocks the move; the preview says what.");
    expect(container.textContent).toContain("Target directory (absolute path)");
    expect(container.querySelector('h2[id$="-move"]')?.textContent).toBe("Move directory");
    expect(container.querySelector('input[placeholder="/absolute/path"]')).not.toBeNull();
    expect(button("Preview move").disabled).toBe(true);
    const label = [...container.querySelectorAll("label")].find((item) => item.textContent === "Default directory")!;
    expect(label.parentElement?.querySelector("p")?.textContent).toBe("Changes the stored path only; files stay where they are. To move the files, use Move directory below.");
    const field = container.querySelector('input[placeholder="/absolute/path"]') as HTMLInputElement;
    const fill = async (value: string) => act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await fill("   ");
    expect(button("Preview move").disabled).toBe(true);
    await fill("/srv/new");
    expect(button("Preview move").disabled).toBe(false);
  });

  it("blocks moving with unsaved settings and allows it after discarding them", async () => {
    await act(async () => root.render(<ProjectSettingsForm projectsRoot="/srv/projects" project={project({ work_dir: "/srv/example" })} onDeleted={() => {}} />));
    const name = container.querySelector("input")!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(name, "Changed name");
      name.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(container.textContent).toContain("Save or discard your changes to the settings before moving the directory.");
    expect(button("Preview move").disabled).toBe(true);
    await act(async () => button("Discard changes").click());
    expect(button("Preview move").disabled).toBe(false);
  });

  it.each([
    { notes: { moved: true, from: "old", to: "new" }, line: "Notes moved" },
    { notes: { moved: false, reason: "they belong to /srv, which does not move" }, line: "Notes not moved: they belong to /srv, which does not move" },
  ])("previews, confirms, refreshes and reports repairs and notes: $line", async ({ notes, line }) => {
    const old = "/srv/example";
    const destination = "/srv/projects/Example";
    const moved = project({ work_dir: destination });
    const impact = `${old} → ${destination}\n1 文件，8 字节；command-pull、agent-check 随目录移动。旧路径保留符号链接，不会中断任何运行。`;
    let complete!: (response: Response) => void;
    const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { complete = resolve; }))
      .mockResolvedValueOnce(Response.json({ status: "ok", result: {
      project: moved, old_dir: old, new_dir: destination, link_old: true,
      repairs: [{ repository: destination, ok: false, output: "repair timed out" }],
      notes,
    } }));
    const previewResponse = Response.json({ status: "ok", result: {
      old_dir: old, new_dir: destination, impact, move_version: "preview-version",
    } });
    vi.stubGlobal("fetch", fetcher);
    const changed = vi.fn();
    window.addEventListener(PROJECTS_CHANGED_EVENT, changed);
    try {
      await act(async () => root.render(<ProjectSettingsForm projectsRoot="/srv/projects" project={project({ work_dir: old })} onDeleted={() => {}} />));
      expect(container.querySelector('[role="switch"][aria-label="Keep a link at the old path"]')?.getAttribute("aria-checked")).toBe("true");
      await act(async () => button("Preview move").click());
      expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/project_admin/preview_move_dir", expect.objectContaining({
        body: JSON.stringify({ project: "project-example", link_old: true }),
      }));
      expect(button("Working…").disabled).toBe(true);
      await act(async () => complete(previewResponse));
      expect(container.textContent).toContain(`${old} → ${destination}`);
      expect(container.textContent).toContain(impact);
      expect(changed).not.toHaveBeenCalled();
      await act(async () => button("Move directory").click());
      expect(fetcher).toHaveBeenLastCalledWith("/api/v1/project_admin/move_dir", expect.objectContaining({
        body: JSON.stringify({ project: "project-example", link_old: true, move_version: "preview-version" }),
      }));
      expect(changed).toHaveBeenCalledOnce();
      expect(container.textContent).toContain(`Moved to ${destination}.`);
      expect(container.querySelector('[role="status"]')?.textContent).toContain(line);
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("Git worktree repair failed: ");
      expect(container.querySelector('[role="alert"]')?.textContent).toContain("repair timed out");
    } finally { window.removeEventListener(PROJECTS_CHANGED_EVENT, changed); }
  });

  it("cancels a preview without moving and disables the move when no directory is configured", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "ok", result: {
      old_dir: "/srv/example", new_dir: "/srv/new", impact: "移动 1 文件", move_version: "v1",
    } }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<ProjectSettingsForm project={project()} onDeleted={() => {}} />));
    expect(button("Preview move").disabled).toBe(true);
    expect(container.textContent).toContain("Set the project's directory first.");
    await act(async () => root.render(<ProjectSettingsForm projectsRoot="/srv/projects" key="configured" project={project({ work_dir: "/srv/example" })} onDeleted={() => {}} />));
    await act(async () => button("Preview move").click());
    await act(async () => button("Cancel").click());
    expect(button("Move directory")).toBeUndefined();
    expect(fetcher).toHaveBeenCalledOnce();
  });

  it("validates absolute destinations, previews link opt-out and reports server refusal without moving", async () => {
    const fetcher = vi.fn(async () => Response.json({ status: "error", error: {
      code: "conflict", message: "项目有运行中的任务，无法移动。",
    } }, { status: 409 }));
    vi.stubGlobal("fetch", fetcher);
    await act(async () => root.render(<ProjectSettingsForm project={project({ work_dir: "/srv/example" })} onDeleted={() => {}} />));
    const field = container.querySelector('input[placeholder="/absolute/path"]') as HTMLInputElement;
    const fill = async (value: string) => act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await fill("relative/path");
    await act(async () => button("Preview move").click());
    expect(fetcher).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("The target must be an absolute path.");
    await fill("/srv/new");
    await act(async () => (container.querySelector('[role="switch"][aria-label="Keep a link at the old path"]') as HTMLButtonElement).click());
    await act(async () => button("Preview move").click());
    expect(fetcher).toHaveBeenCalledExactlyOnceWith("/api/v1/project_admin/preview_move_dir", expect.objectContaining({
      body: JSON.stringify({ project: "project-example", to: "/srv/new", link_old: false }),
    }));
    expect(container.querySelector('[role="alert"]')?.textContent).toBe("项目有运行中的任务，无法移动。");
    expect(button("Move directory")).toBeUndefined();
  });
});
