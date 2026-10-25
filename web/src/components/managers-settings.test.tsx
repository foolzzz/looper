// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManagersSection } from "@/components/managers-settings";
import * as api from "@/lib/api";
import { MANAGER_EXAMPLE } from "@/lib/manager-example";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  fetchManagers: vi.fn(),
  createManager: vi.fn(),
  updateManager: vi.fn(),
  deleteManager: vi.fn(),
  putManagerTemplate: vi.fn(),
  deleteManagerTemplate: vi.fn(),
  fetchProjects: vi.fn(async () => []),
  fetchRoleTemplates: vi.fn(async () => ({ templates: [] })),
  fetchRegistry: vi.fn(async () => ({ profiles: [] })),
}));

vi.mock("@/lib/chat-bindings-api", () => ({ fetchChatBindings: vi.fn(async () => []) }));

const m = vi.mocked(api);

function manager(patch: Partial<api.ManagerInfo> = {}): api.ManagerInfo {
  return {
    id: "manager-review", name: "Review", description: "", builtin: false, enabled: true, tools: "tasks",
    task_scope: "own", task_policy: "free", system_prompt: "Review things.", profile_id: null, profile_name: null,
    templates: [], revision: 0, created_at: "2026-10-24T00:00:00Z", updated_at: "2026-10-24T00:00:00Z",
    state: { running: false, busy: false, healthy: true, sync: "ok" },
    spend: { today_usd: 0, last_7d_usd: 0, runs_today: 0 },
    ...patch,
  };
}

const DEFAULT = manager({ id: "manager", name: "Manager", builtin: true, tools: "all", task_scope: "all", system_prompt: null });

describe("Managers settings", () => {
  let container: HTMLDivElement;
  let root: Root;

  const render = async () => {
    await act(async () => root.render(<ManagersSection />));
  };
  const find = <T extends Element>(selector: string) => container.querySelector(selector) as T;
  const byLabel = (text: string) =>
    Array.from(container.querySelectorAll("label")).find((l) => l.textContent?.startsWith(text))!;
  const field = <T extends HTMLElement>(label: string) =>
    container.querySelector(`#${CSS.escape(byLabel(label).htmlFor)}`) as T;
  const button = (text: string) =>
    Array.from(container.querySelectorAll("button")).find((b) => b.textContent?.includes(text))!;
  const type = async (el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) => {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    await act(async () => {
      Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
      el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
    });
  };
  const click = (el: Element) => act(async () => (el as HTMLElement).click());

  beforeEach(() => {
    vi.clearAllMocks();
    m.fetchManagers.mockResolvedValue([DEFAULT]);
    m.createManager.mockResolvedValue({ manager: manager() });
    m.updateManager.mockResolvedValue({ manager: manager() });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("renders the default Manager without Delete and with the Roles link", async () => {
    await render();
    expect(container.textContent).toContain("all tools");
    expect(container.textContent).toContain("every task");
    expect(container.querySelector('[aria-label^="Delete"]')).toBeNull();
    await click(button("Edit"));
    expect(find<HTMLAnchorElement>('a[href="#settings/roles"]')).not.toBeNull();
    expect(container.textContent).not.toContain("Task visibility");
  });

  it("creates a Manager with tools tasks and own tasks by default", async () => {
    await render();
    await click(button("Add Manager"));
    await type(field("Key"), "review");
    await type(field("Name"), "Review");
    await type(field("Responsibility prompt"), "Review things.");
    await type(field("Tools"), "tasks");
    await click(button("Save"));
    expect(m.createManager).toHaveBeenCalledWith(expect.objectContaining({
      key: "review", name: "Review", system_prompt: "Review things.", tools: "tasks", task_scope: "own",
      task_policy: "free", enabled: true,
    }));
  });

  it("sends tools all with every task", async () => {
    await render();
    await click(button("Add Manager"));
    await type(field("Key"), "wide");
    await type(field("Name"), "Wide");
    await type(field("Responsibility prompt"), "Do all.");
    await type(field("Tools"), "all");
    await type(field("Task visibility"), "all");
    await click(button("Save"));
    expect(m.createManager).toHaveBeenCalledWith(expect.objectContaining({ tools: "all", task_scope: "all" }));
  });

  it("edits own to all and the badge changes after the refetch", async () => {
    m.fetchManagers.mockResolvedValueOnce([DEFAULT, manager()]);
    await render();
    expect(container.textContent).toContain("own tasks");
    m.fetchManagers.mockResolvedValue([DEFAULT, manager({ task_scope: "all" })]);
    await click(find('[aria-label="Edit Manager Review"]'));
    await type(field("Task visibility"), "all");
    await click(button("Save"));
    expect(m.updateManager).toHaveBeenCalledWith("manager-review", { task_scope: "all" });
    expect(container.textContent).not.toContain("own tasks");
  });

  it("shows a pending sync with its reason and clears it on the next fetch", async () => {
    vi.useFakeTimers();
    m.fetchManagers.mockResolvedValueOnce([DEFAULT, manager({
      state: { running: false, busy: false, healthy: true, sync: "pending", sync_error: "database unavailable" },
    })]);
    await render();
    expect(container.textContent).toContain("Not in force yet");
    expect(container.textContent).toContain("database unavailable");
    m.fetchManagers.mockResolvedValue([DEFAULT, manager()]);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(container.textContent).not.toContain("Not in force yet");
  });

  it("shows the server's 400 on template save and keeps the form open", async () => {
    m.fetchManagers.mockResolvedValue([DEFAULT, manager()]);
    m.putManagerTemplate.mockRejectedValue(new Error("unknown placeholder {{x}}"));
    await render();
    await click(button("Add template"));
    await type(field("Key"), "t1");
    await type(field("Name"), "T1");
    await type(field("Requirement"), "Do {{x}}");
    await click(button("Save template"));
    expect(container.textContent).toContain("unknown placeholder {{x}}");
    expect(container.querySelector("form")).not.toBeNull();
  });

  it("fills the create form from the example without saving", async () => {
    await render();
    await click(button("New from example"));
    expect(field<HTMLInputElement>("Name").value).toBe(MANAGER_EXAMPLE.manager.name);
    expect(field<HTMLTextAreaElement>("Responsibility prompt").value).toBe(MANAGER_EXAMPLE.manager.system_prompt);
    expect(m.createManager).not.toHaveBeenCalled();
  });

  it("adds and removes input rows and sends inputs in order", async () => {
    m.fetchManagers.mockResolvedValue([DEFAULT, manager()]);
    m.putManagerTemplate.mockResolvedValue({ managers: [manager()], max_enabled: 8 });
    await render();
    await click(button("Add template"));
    await type(field("Key"), "t1");
    await type(field("Name"), "T1");
    await type(field("Requirement"), "Do {{a}} {{c}}");
    for (let i = 0; i < 3; i++) await click(button("Add input"));
    const keys = () => Array.from(container.querySelectorAll<HTMLInputElement>('input[aria-label="Input key"]'));
    await type(keys()[0], "a");
    await type(keys()[1], "b");
    await type(keys()[2], "c");
    await click(find('[aria-label="Remove input 2"]'));
    expect(keys().map((k) => k.value)).toEqual(["a", "c"]);
    await click(button("Save template"));
    const body = m.putManagerTemplate.mock.calls[0][2];
    expect(m.putManagerTemplate.mock.calls[0].slice(0, 2)).toEqual(["manager-review", "t1"]);
    expect(body.inputs.map((i) => i.key)).toEqual(["a", "c"]);
  });

  it("keeps the example constant equal to the JSON file", async () => {
    // The app's tsconfig has no Node types, so the module name is not a literal.
    const fsModule = "node:fs";
    const fs = await import(/* @vite-ignore */ fsModule);
    // happy-dom's URL cannot build a file: URL, so the path is relative to web/, where vitest runs.
    const file = "../docs/examples/manager-github-pr-review.json";
    expect(MANAGER_EXAMPLE).toEqual(JSON.parse(fs.readFileSync(file, "utf8")));
  });
});
