// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectPage } from "@/pages/project";
import { fetchTasks, type Project, type TaskInfo } from "@/lib/api";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchTasks: vi.fn(),
  // The other overview cards stay loading; only the tasks card is under test.
  fetchKnowledge: vi.fn(() => new Promise(() => {})),
  fetchProjectAgentSettings: vi.fn(() => new Promise(() => {})),
  fetchProjectInstalls: vi.fn(() => new Promise(() => {})),
}));
vi.mock("@/lib/use-projects", () => ({
  useProjects: () => ({
    projects: [{ project_id: "demo", name: "Demo", work_dir: "", task_count: 2, active_count: 0, roles: [] } as unknown as Project],
    projectsRoot: "", error: "", reload: async () => {},
  }),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("project overview task preview", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.clearAllMocks();
  });

  it("asks for tasks without the ones schedules created", async () => {
    const real = { task_id: "real", name: "Real work", status: "completed" } as TaskInfo;
    vi.mocked(fetchTasks).mockResolvedValue({ tasks: [real], next_before: null, counts: { completed: 1 }, hidden_scheduled: 206 });
    await act(async () => root.render(<ProjectPage sub="demo" onNavigate={() => {}} />));
    expect(fetchTasks).toHaveBeenCalledWith({ projectId: "demo", limit: 5, excludeScheduled: true });
    expect(container.textContent).toContain("Real work");
  });

  it("says so when only scheduled tasks exist", async () => {
    vi.mocked(fetchTasks).mockResolvedValue({ tasks: [], next_before: null, counts: {}, hidden_scheduled: 4 });
    await act(async () => root.render(<ProjectPage sub="demo" onNavigate={() => {}} />));
    expect(container.textContent).toContain("Only tasks from schedules so far");
  });
});
