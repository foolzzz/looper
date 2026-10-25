// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { createProject, deleteProject, fetchProjects, PROJECTS_CHANGED_EVENT, updateProject } from "@/lib/api";
import { installSessionGuard, resetSessionGuard, SessionEndedError } from "@/lib/session";

beforeEach(() => resetSessionGuard());
afterEach(() => { resetSessionGuard(); vi.unstubAllGlobals(); });

const project = { project_id: "project-example", name: "Example", description: "Public example", work_dir: "",
  background: "", task_count: 0, active_count: 0, schedule_count: 0, task_workspaces: "worktree" };
const calls = [
  { action: "list", run: () => fetchProjects(), body: {}, result: { projects: [project] }, value: [project], changes: 0 },
  { action: "create", run: () => createProject({ name: "Example", description: "Public example" }),
    body: { name: "Example", description: "Public example" }, result: { project }, value: project, changes: 1 },
  { action: "update", run: () => updateProject(project.project_id, { description: "Public example", background: "" }),
    body: { project_id: project.project_id, fields: { description: "Public example", background: "" } },
    result: { project }, value: project, changes: 1 },
  { action: "delete", run: () => deleteProject(project.project_id), body: { project_id: project.project_id },
    result: { deleted: project.project_id }, value: undefined, changes: 1 },
];

it.each(calls)("project $action uses v1 and preserves its result and selector refresh", async ({ action, run, body, result, value, changes }) => {
  const fetcher = vi.fn(async () => Response.json({ status: "ok", result }));
  vi.stubGlobal("fetch", fetcher);
  const changed = vi.fn();
  window.addEventListener(PROJECTS_CHANGED_EVENT, changed);
  try {
    expect(await run()).toEqual(value);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(`/api/v1/project_admin/${action}`,
      expect.objectContaining({ method: "POST", credentials: "same-origin", body: JSON.stringify(body) }));
    expect(changed).toHaveBeenCalledTimes(changes);
  } finally { window.removeEventListener(PROJECTS_CHANGED_EVENT, changed); }
});

it("preserves the empty project list", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "ok", result: { projects: [] } })));
  expect(await fetchProjects()).toEqual([]);
});

it.each(calls)("project $action preserves typed errors without refreshing selectors", async ({ run }) => {
  vi.stubGlobal("fetch", vi.fn(async () => Response.json({ status: "error", error: {
    code: "conflict", message: "The project is occupied.",
  } }, { status: 409 })));
  const changed = vi.fn();
  window.addEventListener(PROJECTS_CHANGED_EVENT, changed);
  try {
    await expect(run()).rejects.toMatchObject({ name: "V1Error", status: 409, code: "conflict", message: "The project is occupied." });
    expect(changed).not.toHaveBeenCalled();
  } finally { window.removeEventListener(PROJECTS_CHANGED_EVENT, changed); }
});

it.each(calls)("project $action never completes or confirms a pending operation", async ({ run }) => {
  const fetcher = vi.fn(async () => Response.json({ status: "needs_confirmation", confirmation_id: "pending",
    expires_in_seconds: 60, impact: "Remove the empty project." }, { status: 202 }));
  vi.stubGlobal("fetch", fetcher);
  const changed = vi.fn();
  window.addEventListener(PROJECTS_CHANGED_EVENT, changed);
  try {
    await expect(run()).rejects.toMatchObject({ name: "V1Error", status: 202, message: "Remove the empty project." });
    expect(fetcher).toHaveBeenCalledOnce();
    expect(changed).not.toHaveBeenCalled();
  } finally { window.removeEventListener(PROJECTS_CHANGED_EVENT, changed); }
});

it.each(calls)("project $action stops after the operator session ends", async ({ run }) => {
  const fetcher = vi.fn(async () => new Response(null, { status: 401 }));
  vi.stubGlobal("fetch", fetcher);
  installSessionGuard();
  await expect(run()).rejects.toBeInstanceOf(SessionEndedError);
  await expect(run()).rejects.toBeInstanceOf(SessionEndedError);
  expect(fetcher).toHaveBeenCalledOnce();
});
