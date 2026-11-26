// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkRequirement } from "@/components/work-requirement";
import { WorkPage } from "@/pages/work";
import type { RequirementVersion, TaskDetail, TaskStep, UserGate } from "@/lib/api";
import { V1Error } from "@/lib/v1";

const api = vi.hoisted(() => ({
  editTaskRequirement: vi.fn(),
  fetchTaskDetail: vi.fn(),
  fetchTaskActivityWindow: vi.fn(),
  streamTaskActivity: vi.fn(),
  fetchGateThread: vi.fn(),
  postGateMessage: vi.fn(),
  endGateDiscussion: vi.fn(),
  fetchChatMessages: vi.fn(),
  fetchChatTargets: vi.fn(),
  sendChat: vi.fn(),
}));
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), ...api }));
vi.mock("@/components/work-list", () => ({ WorkList: () => <aside>Tasks</aside> }));
vi.mock("@/components/work-switcher", () => ({ WorkSwitcher: () => null }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const EDITABLE = ["draft", "init", "assigned", "running", "planning", "executing", "verifying", "stopping", "waiting_user", "awaiting_budget", "waiting", "pending"];
const READ_ONLY = ["completed", "failed", "stopped", "cancelled", "handing_back", "mystery", ""];

function version(partial: Partial<RequirementVersion> & Pick<RequirementVersion, "version" | "text">): RequirementVersion {
  return { via: "created", created_at: "2026-01-01T00:00:00Z", delivered_at: null, ...partial };
}

function task(partial: Partial<TaskDetail> = {}): TaskDetail {
  return {
    task_id: "task-a", status: "running", group: "in_progress", title: "Parser",
    requirement: "Ship the parser.", requirement_versions: [], ...partial,
  } as TaskDetail;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const fetches: string[] = [];
let host: HTMLDivElement;
let root: Root;

async function settle() {
  for (let i = 0; i < 8; i += 1) await act(async () => { await Promise.resolve(); });
}

function remount() {
  act(() => root.unmount());
  host.remove();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
}

function show(value: TaskDetail, onRefresh: () => Promise<void> = vi.fn()) {
  act(() => { root.render(<WorkRequirement task={value} onRefresh={onRefresh} />); });
  return onRefresh;
}

async function showPage(node: ReactNode) {
  await act(async () => { root.render(node); });
  await settle();
}

function button(label: string) {
  return [...host.querySelectorAll("button")].find((el) => el.textContent === label) as HTMLButtonElement | undefined;
}

async function click(label: string) {
  const el = button(label);
  expect(el).toBeTruthy();
  await act(async () => { el!.click(); });
}

async function typeInto(value: string) {
  const input = host.querySelector('[aria-label="需求正文"]');
  expect(input).toBeTruthy();
  await act(async () => {
    const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    set!.call(input, value);
    input!.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  fetches.length = 0;
  api.editTaskRequirement.mockReset();
  vi.stubGlobal("fetch", (url: string) => {
    fetches.push(String(url));
    throw new Error("requirement must not request");
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("sends the version the editor opened with even after a newer detail arrives", async () => {
  const initial = task({ requirement_versions: [version({ version: 5, text: "Ship the parser." })] });
  show(initial);
  await click("编辑");
  await typeInto("My edit");
  show({ ...initial, requirement: "Other edit", requirement_versions: [version({ version: 6, text: "Other edit" })] });
  api.editTaskRequirement.mockRejectedValue(new V1Error(409, "requirement_changed", "Changed elsewhere"));
  await click("保存");
  expect(api.editTaskRequirement).toHaveBeenCalledWith("task-a", "My edit", 5);
  expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("My edit");
  expect(button("保存")!.disabled).toBe(false);
});

describe("Work requirement text and versions", () => {
  it("shows the original text, and an empty requirement without inventing a version", () => {
    show(task({ requirement: "Ship the parser.\nKeep this line." }));
    expect(host.querySelector("#work-requirement-title")?.textContent).toBe("需求");
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("Ship the parser.\nKeep this line.");
    expect(host.querySelector("[data-requirement-version]")).toBeNull();
    expect(host.querySelector("[data-requirement-history]")).toBeNull();
    expect(host.textContent).not.toContain("尚未填写需求");
    expect(host.textContent).not.toContain("规划者尚未读取");
    expect(fetches).toEqual([]);

    show(task({ requirement: "" }));
    expect(host.textContent).toContain("尚未填写需求");
    expect(host.querySelector("[data-requirement-body]")).toBeNull();
    expect(host.querySelector("[data-requirement-version]")).toBeNull();

    show(task({ requirement: undefined as unknown as string }));
    expect(host.textContent).toContain("尚未填写需求");

    show(task({ requirement: "   " }));
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("   ");
    expect(host.textContent).not.toContain("尚未填写需求");
  });

  it("shows one real version without a history list", () => {
    show(task({
      requirement: "Ship the parser.",
      requirement_versions: [version({ version: 1, text: "Ship the parser.", delivered_at: null })],
    }));
    expect(host.querySelector("[data-requirement-version]")?.textContent).toContain("版本 1");
    expect(host.querySelector("[data-requirement-version]")?.textContent).not.toContain("规划者尚未读取");
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("Ship the parser.");
    expect(host.querySelector("[data-requirement-history]")).toBeNull();
    expect(host.textContent).not.toContain("已读取");
  });

  it("lists many versions newest first with the original text, source, time and delivery", () => {
    const tail = `TAIL-${"q".repeat(300)}`;
    show(task({
      requirement: "Current body stays on the task.",
      requirement_versions: [
        version({ version: 1, text: "First original.", via: "created", created_at: "2026-01-01T00:00:00Z", delivered_at: null }),
        version({ version: 3, text: `Third original.\n${tail}`, via: "task_page", created_at: "2026-03-01T00:00:00Z", delivered_at: null }),
        version({ version: 2, text: "Second original.", via: "manager:web", created_at: "2026-02-01T00:00:00Z", delivered_at: "2026-02-02T03:04:05Z" }),
      ],
    }));
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("Current body stays on the task.");
    expect(host.querySelector("[data-requirement-version]")?.textContent).toContain("版本 3");
    expect(host.querySelector("[data-requirement-version]")?.textContent).toContain("规划者尚未读取");
    const rows = [...host.querySelectorAll("[data-version]")];
    expect(rows.map((row) => row.getAttribute("data-version"))).toEqual(["3", "2", "1"]);
    expect(rows[0].textContent).toContain(`Third original.\n${tail}`);
    expect(rows[0].textContent).toContain("来源 task_page");
    expect(rows[0].textContent).toContain("2026-03-01T00:00:00Z");
    expect(rows[0].textContent).toContain("规划者尚未读取");
    expect(rows[0].textContent).not.toContain("已读取");
    expect(rows[1].textContent).toContain("Second original.");
    expect(rows[1].textContent).toContain("来源 manager:web");
    expect(rows[1].textContent).toContain("2026-02-01T00:00:00Z");
    expect(rows[1].textContent).toContain("2026-02-02T03:04:05Z");
    expect(rows[1].querySelector("time[datetime='2026-02-02T03:04:05Z']")).not.toBeNull();
    expect(rows[1].textContent).not.toContain("规划者尚未读取");
    expect(rows[1].textContent).not.toContain("已读取");
    expect(rows[2].textContent).toContain("First original.");
    expect(rows[2].textContent).toContain("来源 created");
    expect(rows[2].textContent).not.toContain("规划者尚未读取");
    expect(host.textContent).not.toContain("已保存");
  });
});

describe("Work requirement editing", () => {
  it.each(EDITABLE)("can edit a %s task", (status) => {
    show(task({ status }));
    const edit = button("编辑");
    expect(edit).toBeTruthy();
    expect(edit!.className).toContain("border-border");
    expect(host.querySelector("textarea")).toBeNull();
  });

  it.each(READ_ONLY)("is read-only for %j", (status) => {
    show(task({ status }));
    expect(button("编辑")).toBeUndefined();
    expect(button("保存")).toBeUndefined();
    expect(host.querySelector("textarea")).toBeNull();
  });

  it("refuses a blank or unchanged requirement and restores the original on cancel", async () => {
    show(task());
    await click("编辑");
    expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("Ship the parser.");
    expect(button("保存")!.disabled).toBe(true);
    await act(async () => { button("保存")!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(api.editTaskRequirement).not.toHaveBeenCalled();

    await typeInto("Ship the parser.");
    expect(button("保存")!.disabled).toBe(true);
    await typeInto("   ");
    expect(button("保存")!.disabled).toBe(true);
    await act(async () => { button("保存")!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(api.editTaskRequirement).not.toHaveBeenCalled();

    await typeInto("  Ship the parser.  ");
    expect(button("保存")!.disabled).toBe(false);
    await click("取消");
    expect(host.querySelector("textarea")).toBeNull();
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("Ship the parser.");
    expect(api.editTaskRequirement).not.toHaveBeenCalled();
    await click("编辑");
    expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("Ship the parser.");
  });

  it("sends the raw text once while saving, and keeps it when the save fails", async () => {
    const refresh = vi.fn(() => Promise.resolve());
    const gate = deferred<void>();
    api.editTaskRequirement.mockReturnValue(gate.promise);
    show(task(), refresh);
    await click("编辑");
    await typeInto("  Ship the parser.  ");
    await act(async () => {
      const save = button("保存")!;
      save.click();
      save.click();
    });
    expect(api.editTaskRequirement).toHaveBeenCalledTimes(1);
    expect(api.editTaskRequirement).toHaveBeenCalledWith("task-a", "  Ship the parser.  ", 1);
    expect(button("保存中…")!.disabled).toBe(true);
    expect(button("保存")).toBeUndefined();
    await act(async () => { button("保存中…")!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(api.editTaskRequirement).toHaveBeenCalledTimes(1);

    gate.reject(new Error("requirement must be a non-empty string"));
    await act(async () => { await gate.promise.catch(() => {}); });
    await settle();
    expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("  Ship the parser.  ");
    expect(host.textContent).toContain("requirement must be a non-empty string");
    expect(refresh).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain("已保存");
    expect(host.textContent).not.toContain("已读取");
  });

  it("shows a new version only after the task is read back", async () => {
    const refresh = vi.fn(() => Promise.resolve());
    show(task({ requirement_versions: [version({ version: 1, text: "Ship the parser." })] }), refresh);
    await click("编辑");
    await typeInto("Ship the parser.\nAdd a cache.");
    api.editTaskRequirement.mockResolvedValue(undefined);
    await click("保存");
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(host.textContent).not.toContain("版本 2");
    expect(host.textContent).not.toContain("已保存");
    expect(host.querySelector("textarea")).toBeNull();
    show(task({
      requirement: "Ship the parser.\nAdd a cache.",
      requirement_versions: [
        version({ version: 1, text: "Ship the parser." }),
        version({ version: 2, text: "Ship the parser.\nAdd a cache.", via: "task_page", created_at: "2026-04-01T00:00:00Z", delivered_at: null }),
      ],
    }), refresh);
    expect(host.querySelector("[data-version='2']")?.textContent).toContain("Ship the parser.\nAdd a cache.");
    expect(host.querySelector("[data-version='2']")?.textContent).toContain("规划者尚未读取");
    expect(host.textContent).not.toContain("已读取");
  });

  it("keeps the draft when the readback fails", async () => {
    const refresh = vi.fn(() => Promise.reject(new Error("任务详情与当前任务不匹配，请刷新")));
    show(task(), refresh);
    await click("编辑");
    await typeInto("Ship the parser.\nAdd a cache.");
    api.editTaskRequirement.mockResolvedValue(undefined);
    await click("保存");
    await settle();
    expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("Ship the parser.\nAdd a cache.");
    expect(host.textContent).toContain("任务详情与当前任务不匹配，请刷新");
    expect(host.textContent).not.toContain("版本 2");
    expect(host.textContent).not.toContain("已保存");
  });

  it("does not report success when confirmation is still required", async () => {
    const refresh = vi.fn(() => Promise.resolve());
    api.editTaskRequirement.mockRejectedValue(new V1Error(202, "unavailable", "The Planner will re-plan this task."));
    show(task(), refresh);
    await click("编辑");
    await typeInto("Ship the parser.\nAdd a cache.");
    await click("保存");
    await settle();
    expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("Ship the parser.\nAdd a cache.");
    expect(host.textContent).toContain("The Planner will re-plan this task.");
    expect(refresh).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain("已保存");
    expect(host.textContent).not.toContain("已读取");
  });

  it("stops asking for a missing edit route and still retries a missing task", async () => {
    const refresh = vi.fn(() => Promise.resolve());
    api.editTaskRequirement.mockRejectedValue(new V1Error(404, "not_found", "no POST /api/v1/tasks/requirement_update"));
    show(task(), refresh);
    await click("编辑");
    await typeInto("Ship the parser.\nAdd a cache.");
    await click("保存");
    await settle();
    expect(host.textContent).toContain("需求编辑暂不可用");
    expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("Ship the parser.\nAdd a cache.");
    await act(async () => { button("保存")!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(api.editTaskRequirement).toHaveBeenCalledTimes(1);
    expect(refresh).not.toHaveBeenCalled();

    show(task({ task_id: "task-b", requirement: "Other task." }), refresh);
    expect(host.textContent).toContain("需求编辑暂不可用");
    expect(button("编辑")).toBeUndefined();
    expect(host.textContent).toContain("Other task.");
    expect(host.textContent).not.toContain("Add a cache.");

    // A missing task is a different 404. A fresh section still edits, and one refusal does not retire the route.
    remount();
    api.editTaskRequirement.mockRejectedValue(new V1Error(404, "not_found", "task not found"));
    show(task({ task_id: "task-c", requirement: "Third." }), refresh);
    await click("编辑");
    await typeInto("Third.\nMore.");
    await click("保存");
    await settle();
    expect(host.textContent).toContain("task not found");
    expect(host.textContent).not.toContain("需求编辑暂不可用");
    api.editTaskRequirement.mockResolvedValue(undefined);
    await click("保存");
    expect(api.editTaskRequirement).toHaveBeenCalledTimes(3);
  });

  it("does not poll the edit route", async () => {
    vi.useFakeTimers();
    show(task());
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(api.editTaskRequirement).not.toHaveBeenCalled();
    expect(fetches).toEqual([]);
  });

  it("makes the open editor read-only when the same task becomes terminal or unknown", async () => {
    api.editTaskRequirement.mockResolvedValue(undefined);
    show(task({ status: "running", requirement: "Ship the parser." }));
    await click("编辑");
    await typeInto("Ship the parser.\nMore.");
    expect(button("保存")!.disabled).toBe(false);

    for (const status of ["completed", "mystery", ""]) {
      show(task({ task_id: "task-a", status, requirement: "Ship the parser." }));
      const area = host.querySelector('[aria-label="需求正文"]') as HTMLTextAreaElement | null;
      expect(area, status).toBeTruthy();
      expect(area!.readOnly).toBe(true);
      const save = button("保存");
      expect(save, status).toBeTruthy();
      expect(save!.disabled).toBe(true);
      expect(button("编辑")).toBeUndefined();
      await act(async () => { save!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
      expect(api.editTaskRequirement).not.toHaveBeenCalled();
    }
  });

  it("keeps cancel and reopening locked until both the save and readback settle", async () => {
    const first = deferred<void>();
    const readback = deferred<void>();
    api.editTaskRequirement.mockImplementationOnce(() => first.promise);
    api.editTaskRequirement.mockImplementationOnce(() => Promise.resolve());
    const refresh = vi.fn(() => readback.promise);
    show(task(), refresh);
    await click("编辑");
    await typeInto("first edit");
    await click("保存");
    expect(api.editTaskRequirement).toHaveBeenCalledTimes(1);

    const cancel = button("取消")!;
    expect(cancel.disabled).toBe(true);
    await act(async () => { cancel.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
    expect(button("保存中…")).toBeTruthy();
    expect(button("编辑")).toBeUndefined();
    expect((host.querySelector('[aria-label="需求正文"]') as HTMLTextAreaElement).value).toBe("first edit");
    await click("保存中…");
    expect(api.editTaskRequirement).toHaveBeenCalledTimes(1);

    first.resolve();
    await act(async () => { await first.promise; });
    await settle();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(button("取消")!.disabled).toBe(true);
    expect(button("保存中…")!.disabled).toBe(true);
    readback.resolve();
    await settle();
    expect(host.querySelector("textarea")).toBeNull();
    await click("编辑");
    await typeInto("second edit");
    await click("保存");
    expect(api.editTaskRequirement).toHaveBeenCalledTimes(2);
  });
});

describe("Work requirement task switches", () => {
  it("does not refresh a newer edit when the same task is selected again before its old save returns", async () => {
    const oldSave = deferred<void>();
    api.editTaskRequirement.mockReturnValue(oldSave.promise);
    const refresh = vi.fn(() => Promise.resolve());
    show(task(), refresh);
    await click("编辑");
    await typeInto("old edit");
    await click("保存");
    show(task({ task_id: "B" }), refresh);
    show(task(), refresh);
    await click("编辑");
    await typeInto("new edit");
    oldSave.resolve();
    await settle();
    expect(refresh).not.toHaveBeenCalled();
    expect((host.querySelector("textarea") as HTMLTextAreaElement).value).toBe("new edit");
    expect(button("保存")!.disabled).toBe(false);
  });

  it("drops task A's late edit before it can change task B", async () => {
    const gate = deferred<void>();
    const refresh = vi.fn(() => Promise.resolve());
    api.editTaskRequirement.mockReturnValue(gate.promise);
    show(task({ task_id: "A", requirement: "alpha" }), refresh);
    await click("编辑");
    await typeInto("alpha changed");
    await act(async () => { button("保存")!.click(); });
    show(task({ task_id: "B", requirement: "beta" }), refresh);
    expect(host.querySelector("textarea")).toBeNull();
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("beta");
    gate.resolve();
    await act(async () => { await gate.promise; });
    await settle();
    expect(refresh).not.toHaveBeenCalled();
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("beta");
    expect(host.textContent).not.toContain("alpha");
    expect(host.querySelector("textarea")).toBeNull();
  });

  it("does not apply task A's failed readback after the page shows task B", async () => {
    const gate = deferred<void>();
    const refreshA = vi.fn(() => gate.promise);
    const refreshB = vi.fn(() => Promise.resolve());
    api.editTaskRequirement.mockResolvedValue(undefined);
    show(task({ task_id: "A", requirement: "alpha" }), refreshA);
    await click("编辑");
    await typeInto("alpha changed");
    await click("保存");
    expect(refreshA).toHaveBeenCalledTimes(1);
    show(task({ task_id: "B", requirement: "beta" }), refreshB);
    gate.reject(new Error("stale A"));
    await act(async () => { await gate.promise.catch(() => {}); });
    await settle();
    expect(refreshB).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain("stale A");
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("beta");
    expect(host.textContent).not.toContain("alpha");
  });
});

describe("Work requirement on the page", () => {
  function media(wide: boolean) {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: wide && query === "(min-width: 1280px)",
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
  }

  beforeEach(() => {
    api.fetchTaskDetail.mockReset();
    api.fetchTaskActivityWindow.mockReset();
    api.streamTaskActivity.mockReset();
    api.fetchGateThread.mockReset();
    api.fetchChatMessages.mockReset();
    api.fetchChatTargets.mockReset();
    api.fetchTaskDetail.mockResolvedValue(task());
    api.fetchTaskActivityWindow.mockResolvedValue({ events: [], hasMore: false });
    api.streamTaskActivity.mockReturnValue(() => {});
    api.fetchGateThread.mockResolvedValue({ messages: [] });
    api.fetchChatMessages.mockResolvedValue([]);
    api.fetchChatTargets.mockResolvedValue([]);
    api.postGateMessage.mockReset();
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      fetches.push(String(url));
      return Response.json({ status: "ok", result: { activity: [{ seq: 1 }] } });
    }));
  });

  it("mounts one requirement section inside the wide column and none while the narrow panel is closed", async () => {
    media(false);
    window.innerWidth = 390;
    await showPage(<WorkPage sub="task/task-a" onNavigate={vi.fn()} />);
    expect(host.querySelector("#work-requirement-title")).toBeNull();
    expect(api.editTaskRequirement).not.toHaveBeenCalled();
    expect(fetches.some((url) => url.includes("requirement_update"))).toBe(false);
    await click("任务详情");
    const dialog = host.querySelector('[role="dialog"]') as HTMLElement;
    expect(dialog.querySelector("button")?.textContent).toBe("关闭任务详情");
    expect(dialog.querySelector("#work-requirement-title")?.textContent).toBe("需求");
    expect(dialog.querySelector("[data-requirement-body]")?.textContent).toBe("Ship the parser.");
    expect(api.editTaskRequirement).not.toHaveBeenCalled();

    media(true);
    window.innerWidth = 1280;
    remount();
    await showPage(<WorkPage sub="task/task-a" onNavigate={vi.fn()} />);
    const panel = host.querySelector('aside[aria-label="任务详情"]') as HTMLElement;
    expect(host.querySelector('[role="dialog"]')).toBeNull();
    const requirement = panel.querySelector("#work-requirement-title") as HTMLElement;
    const steps = panel.querySelector("#work-steps-title") as HTMLElement;
    expect(requirement.textContent).toBe("需求");
    expect(requirement.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(panel.querySelector('[aria-label="关卡"]')).not.toBeNull();
    expect(api.editTaskRequirement).not.toHaveBeenCalled();
    expect(fetches.some((url) => url.includes("requirement_update"))).toBe(false);
  });

  it("does not let task A's late readback replace task B", async () => {
    media(true);
    window.innerWidth = 1280;
    api.fetchTaskDetail.mockResolvedValue(task({ task_id: "A", requirement: "alpha", title: "Task A" }));
    await showPage(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("alpha");
    const late = deferred<TaskDetail>();
    api.fetchTaskDetail.mockClear();
    api.fetchTaskDetail.mockImplementation((id: string) => (
      id === "B" ? Promise.resolve(task({ task_id: "B", requirement: "beta", title: "Task B" })) : late.promise
    ));
    api.editTaskRequirement.mockResolvedValue(undefined);
    await click("编辑");
    await typeInto("alpha changed");
    await click("保存");
    expect(api.fetchTaskDetail).toHaveBeenCalledWith("A");
    expect(api.editTaskRequirement).toHaveBeenCalledWith("A", "alpha changed", 1);
    await showPage(<WorkPage sub="task/B" onNavigate={vi.fn()} />);
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("beta");
    late.resolve(task({
      task_id: "A", requirement: "ALPHA-LATE", title: "Task A",
      requirement_versions: [version({ version: 2, text: "ALPHA-LATE", via: "task_page" })],
    }));
    await settle();
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("beta");
    expect(host.textContent).not.toContain("ALPHA-LATE");
    expect(host.textContent).not.toContain("alpha changed");
  });

  function holdDetails() {
    const pending: { id: string; resolve: (task: TaskDetail) => void }[] = [];
    api.fetchTaskDetail.mockImplementation((id: string) => new Promise(resolve => {
      pending.push({ id, resolve });
    }));
    return pending;
  }

  async function releaseOldest(pending: { id: string; resolve: (task: TaskDetail) => void }[], value: TaskDetail) {
    const item = pending.shift();
    expect(item).toBeTruthy();
    await act(async () => { item!.resolve(value); });
    await settle();
  }

  async function releaseAt(
    pending: { id: string; resolve: (task: TaskDetail) => void }[],
    index: number,
    value: TaskDetail,
  ) {
    const [item] = pending.splice(index, 1);
    expect(item).toBeTruthy();
    await act(async () => { item!.resolve(value); });
    await settle();
  }

  function openGate(topic: string): UserGate {
    return {
      step: "decision", step_id: "gate-step", state: "open", topic, findings: "", summary: null,
      resolution: "", agenda: [], ended_by_user: false, plan_change: null, depends_on: [],
      waiting_on: "user", not_delivered: 0, retry_at: null, turns: 0, reminders: 0,
      opened_at: "2026-11-01T10:00:00Z", first_reply_at: null, closed_at: null, channel_posts: null,
    };
  }

  function workStep(instruction: string): TaskStep {
    return {
      id: "s1", task_id: "A", kind: "work", step_key: "implement", step_index: 0, instruction,
      acceptance_criteria: [], target_files: [], depends_on: [], status: "pending", review: false,
      role_key: null, role_version: null, review_roles: [], review_stage: null, review_position: null,
      retries: 0, worker_id: null, reviewer_id: null, result: null, created_at: "2026-01-01T00:00:00Z",
      updated_at: "2026-01-01T00:00:00Z", completed_at: null, started_at: null,
    };
  }

  function detailAt(versionNo: number, topic: string, instruction: string): TaskDetail {
    return task({
      task_id: "A", title: "Task A", status: "running", group: "in_progress",
      requirement: `body-v${versionNo}`,
      requirement_versions: [version({ version: versionNo, text: `body-v${versionNo}` })],
      gates: [openGate(topic)],
      steps: [workStep(instruction)],
    });
  }

  function panel() {
    return host.querySelector('aside[aria-label="任务详情"]');
  }

  async function showSettled(versionNo: number, topic: string, instruction: string) {
    vi.useFakeTimers();
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    media(true);
    window.innerWidth = 1280;
    const pending = holdDetails();
    const value = detailAt(versionNo, topic, instruction);
    await showPage(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    while (!host.querySelector("[data-requirement-version]")?.textContent?.includes(`版本 ${versionNo}`)) {
      expect(pending.length).toBeGreaterThan(0);
      await releaseOldest(pending, value);
    }
    while (pending.length) await releaseOldest(pending, value);
    expect(panel()?.textContent).toContain(topic);
    expect(panel()?.textContent).toContain(instruction);
    return pending;
  }

  it("does not let a late save of task A refresh or invalidate an uncached task B", async () => {
    media(true);
    window.innerWidth = 1280;
    api.fetchTaskDetail.mockImplementation(async (id: string) => task({
      task_id: id, requirement: id === "A" ? "alpha" : "beta", title: id === "A" ? "Task A" : "Task B",
    }));
    await showPage(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("alpha");

    const save = deferred<void>();
    api.editTaskRequirement.mockReturnValue(save.promise);
    const loadB = deferred<TaskDetail>();
    api.fetchTaskDetail.mockImplementation((id: string) => (
      id === "B" ? loadB.promise : Promise.resolve(task({ task_id: id, requirement: "alpha-late", title: "Task A" }))
    ));
    await click("编辑");
    await typeInto("alpha changed");
    await click("保存");
    await showPage(<WorkPage sub="task/B" onNavigate={vi.fn()} />);
    expect(host.textContent).toContain("Loading task…");
    const callsAtSwitch = api.fetchTaskDetail.mock.calls.length;
    save.resolve();
    await act(async () => { await save.promise; });
    await settle();
    expect(api.fetchTaskDetail.mock.calls.slice(callsAtSwitch).map(call => call[0])).not.toContain("A");
    loadB.resolve(task({ task_id: "B", requirement: "beta", title: "Task B" }));
    await settle();
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("beta");
    expect(host.textContent).not.toContain("Loading task…");
    expect(host.textContent).not.toContain("alpha-late");
    expect(host.textContent).not.toContain("alpha changed");
  });

  it("does not read the task back after leaving Work", async () => {
    media(true);
    window.innerWidth = 1280;
    api.fetchTaskDetail.mockResolvedValue(task({ task_id: "A", requirement: "alpha", title: "Task A" }));
    await showPage(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(host.querySelector("[data-requirement-body]")?.textContent).toBe("alpha");
    const save = deferred<void>();
    api.editTaskRequirement.mockReturnValue(save.promise);
    api.fetchTaskDetail.mockClear();
    await click("编辑");
    await typeInto("alpha changed");
    await click("保存");
    expect(api.fetchTaskDetail).not.toHaveBeenCalled();
    await act(async () => { root.unmount(); });
    root = createRoot(host);
    save.resolve();
    await act(async () => { await save.promise; });
    await settle();
    expect(api.fetchTaskDetail).not.toHaveBeenCalled();
  });

  it("does not let an older requirement readback replace a newer shared poll", async () => {
    const pending = await showSettled(1, "topic-v1", "step-v1");
    api.editTaskRequirement.mockResolvedValue(undefined);
    await click("编辑");
    await typeInto("body-v1\nedited");
    await click("保存");
    expect(pending).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(pending.map(item => item.id)).toEqual(["A", "A"]);
    // The poll started second. It publishes version 3 while the older requirement readback is still in flight.
    await releaseAt(pending, 1, detailAt(3, "topic-v3", "step-v3"));
    expect(host.querySelector("[data-requirement-version]")?.textContent).toContain("版本 3");
    expect(panel()?.textContent).toContain("topic-v3");
    expect(panel()?.textContent).toContain("step-v3");
    await releaseAt(pending, 0, detailAt(2, "topic-v2", "step-v2"));
    await settle();
    expect(host.querySelector("[data-requirement-version]")?.textContent).toContain("版本 3");
    expect(panel()?.textContent).toContain("topic-v3");
    expect(panel()?.textContent).toContain("step-v3");
    expect(panel()?.textContent).not.toContain("topic-v2");
    expect(panel()?.textContent).not.toContain("step-v2");
    expect(host.textContent).not.toContain("body-v2");

    await showPage(<WorkPage sub="task/B" onNavigate={vi.fn()} />);
    await showPage(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(host.querySelector("[data-requirement-version]")?.textContent).toContain("版本 3");
    expect(panel()?.textContent).toContain("topic-v3");
    expect(panel()?.textContent).toContain("step-v3");
    expect(panel()?.textContent).not.toContain("topic-v2");
    expect(host.textContent).not.toContain("body-v2");
  });

  it.each(["completed", "mystery"])("revokes an open editor when the shared poll reports %s", async (status) => {
    const pending = await showSettled(1, "topic-v1", "step-v1");
    await click("编辑");
    await typeInto("draft before transition");
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(pending).toHaveLength(1);
    await releaseOldest(pending, { ...detailAt(2, "topic-v2", "step-v2"), status });
    expect((host.querySelector('[aria-label="需求正文"]') as HTMLTextAreaElement).readOnly).toBe(true);
    expect(button("保存")!.disabled).toBe(true);
    await click("保存");
    expect(api.editTaskRequirement).not.toHaveBeenCalled();
  });

  it("does not let an older shared poll replace a newer requirement readback or its cache", async () => {
    const pending = await showSettled(1, "topic-v1", "step-v1");
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(pending).toHaveLength(1);
    api.editTaskRequirement.mockResolvedValue(undefined);
    await click("编辑");
    await typeInto("edited");
    await click("保存");
    expect(pending).toHaveLength(2);
    await releaseAt(pending, 1, detailAt(3, "topic-v3", "step-v3"));
    await releaseOldest(pending, detailAt(2, "topic-v2", "step-v2"));
    expect(host.querySelector("[data-requirement-version]")?.textContent).toContain("版本 3");
    expect(panel()?.textContent).toContain("topic-v3");
    expect(panel()?.textContent).toContain("step-v3");
    await showPage(<WorkPage sub="task/B" onNavigate={vi.fn()} />);
    await showPage(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(host.querySelector("[data-requirement-version]")?.textContent).toContain("版本 3");
    expect(panel()?.textContent).toContain("topic-v3");
  });

  it("does not let an older requirement readback replace a newer gate update", async () => {
    const pending = await showSettled(1, "topic-v1", "step-v1");
    const updated = openGate("topic-from-gate");
    api.postGateMessage.mockResolvedValue({
      gate: updated,
      message: { id: 1, role: "user", text: "ship it", created_at: "2026-11-01T10:00:00Z" },
      delivered: "sent",
    });
    api.editTaskRequirement.mockResolvedValue(undefined);
    await click("编辑");
    await typeInto("body-v1\nedited");
    await click("保存");
    expect(pending).toHaveLength(1);

    const input = host.querySelector("#gate-decision-text") as HTMLTextAreaElement;
    expect(input).toBeTruthy();
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
      set.call(input, "ship it");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const send = input.closest("form")!.querySelector('button[type="submit"]') as HTMLButtonElement;
    await act(async () => { send.click(); });
    await settle();
    expect(api.postGateMessage).toHaveBeenCalledWith("A", "decision", { text: "ship it" });
    expect(panel()?.textContent).toContain("topic-from-gate");
    expect(host.textContent).toContain("topic-from-gate");

    await releaseOldest(pending, detailAt(2, "topic-v2", "step-v2"));
    await settle();
    expect(panel()?.textContent).toContain("topic-from-gate");
    expect(panel()?.textContent).toContain("step-v1");
    expect(panel()?.textContent).not.toContain("topic-v2");
    expect(panel()?.textContent).not.toContain("step-v2");
    expect(host.textContent).not.toContain("body-v2");
    expect(host.textContent).toContain("topic-from-gate");

    await showPage(<WorkPage sub="task/B" onNavigate={vi.fn()} />);
    await showPage(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(panel()?.textContent).toContain("topic-from-gate");
    expect(panel()?.textContent).not.toContain("topic-v1");
    expect(panel()?.textContent).not.toContain("topic-v2");
  });
});
