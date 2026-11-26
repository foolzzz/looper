// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { taskConversationHint } from "@/components/work-conversation";
import { WorkPage } from "@/pages/work";

vi.mock("@/lib/use-projects", () => ({ useProjects: () => ({ projects: [] }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
let task: Record<string, unknown>;
let refuse: string;
let pendingConfirmation: boolean;
let failReadback: boolean;
let releaseWrite: (() => Promise<void>) | undefined;
let plannerAvailable: boolean;
const writes: { url: string; body: Record<string, unknown> }[] = [];
const settle = async () => { for (let n = 0; n < 15; n++) await act(async () => { await Promise.resolve(); }); };
const button = (label: string) => [...host.querySelectorAll("button")].find(item => item.textContent === label)!;
const input = () => host.querySelector('textarea')!;
async function send(text: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input(), text);
    input().dispatchEvent(new Event("input", { bubbles: true }));
  });
  act(() => input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })));
  await settle();
}
async function show() {
  await act(async () => root.render(<WorkPage sub="task/task-draft" onNavigate={() => {}} />));
  await settle();
}

beforeEach(() => {
  sessionStorage.clear(); localStorage.clear(); writes.length = 0;
  refuse = ""; pendingConfirmation = false; failReadback = false; releaseWrite = undefined;
  plannerAvailable = true;
  task = { task_id: "task-draft", title: "", status: "draft", requirement: "", requirement_versions: [],
    created_at: "2026-11-25T10:45:00Z", work_dir: "workspace", project_id: "p", project_name: "Demo",
    steps: [], gates: [], cost: { runs: 0, running: 0 } };
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    const url = String(path);
    const body = JSON.parse(String(init?.body ?? "{}"));
    let result: unknown = {};
    let status = 200;
    if (url.includes("requirement_update")) {
      writes.push({ url, body });
      await releaseWrite?.();
      if (pendingConfirmation) return new Response(JSON.stringify({ status: "needs_confirmation", impact: "Operator confirmation required", confirmation_id: "pending", expires_in_seconds: 60 }), { status: 202 });
      if (refuse) return new Response(JSON.stringify({ status: "error", error: { code: "conflict", message: refuse } }), { status: 409 });
      const versions = task.requirement_versions as { version: number; text: string }[];
      const current = Math.max(1, ...versions.map(item => item.version));
      if (body.expected_version !== undefined && body.expected_version !== current)
        return new Response(JSON.stringify({ status: "error", error: { code: "requirement_changed", message: "Changed elsewhere", details: { current_version: current } } }), { status: 409 });
      const version = current + 1;
      task = { ...task, requirement: body.requirement, requirement_versions: [...versions, { version, text: body.requirement, via: "operator" }] };
      result = { task_id: task.task_id, version, requirement: body.requirement };
    } else if (url.includes("task_control")) {
      writes.push({ url, body });
      if (refuse) return new Response(JSON.stringify({ status: "error", error: { code: "conflict", message: refuse } }), { status: 409 });
      task = { ...task, status: "running" }; result = { task_id: task.task_id, started: true };
    } else if (url.includes("tasks/detail")) {
      if (failReadback && writes.length) throw new TypeError("Readback offline");
      result = task;
    } else if (url.includes("work_list")) result = { items: body.section === "open" ? [{ ...task, title: task.title || task.task_id, last_seq: 0 }] : [], schedules: [], next_cursor: null };
    else if (url.includes("tasks/activity")) result = { activity: [] };
    else if (url.includes("chat/targets")) result = { targets: [{ id: "planner-task-draft", kind: "planner", task_id: "task-draft", status: task.status,
      can_send: plannerAvailable && ["pending", "assigned", "running", "planning", "executing", "verifying", "stopping", "waiting", "waiting_user", "awaiting_budget"].includes(String(task.status)) }] };
    else if (url.includes("chat/messages")) result = { messages: [] };
    else if (url.includes("task_conversations")) result = { enabled: false };
    if (init?.method === "GET") status = 405;
    return new Response(JSON.stringify({ status: "ok", result }), { status, headers: { "Content-Type": "application/json" } });
  }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it.each(["draft", "init"])("appends two %s messages through requirement_update and restores the server text and version on reload", async status => {
  const initial = status === "init" ? "Existing requirement\n\n" : "";
  task = { ...task, status, requirement: initial.trim() };
  await show();
  expect(input().disabled).toBe(false);
  expect(button("开始").disabled).toBe(status === "draft");
  expect(host.querySelector('[data-work-detail]')?.textContent).not.toContain("任务已结束");
  if (status === "init") expect(host.querySelector('[aria-label="需求草稿"]')?.textContent).toContain("版本 1");
  await send("First requirement");
  expect(writes[0]).toEqual({ url: "/api/v1/tasks/requirement_update", body: { task_id: "task-draft", requirement: `${initial}First requirement`, expected_version: 1 } });
  expect(host.querySelector('[aria-label="需求草稿"]')?.textContent).toContain("First requirement");
  expect(input().value).toBe("");
  expect(button("开始").disabled).toBe(false);
  await send("Second requirement");
  expect(writes[1].body).toMatchObject({ requirement: `${initial}First requirement\n\nSecond requirement`, expected_version: 2 });
  expect(writes).toHaveLength(2);
  act(() => root.unmount()); root = createRoot(host); await show();
  const draft = host.querySelector('[aria-label="需求草稿"]')!;
  expect(draft.textContent).toContain("First requirement\n\nSecond requirement");
  expect(draft.textContent).toContain("版本 3");
  expect(writes.some(write => write.url.includes("task_control"))).toBe(false);
});

it.each([false, true])("keeps a rejected draft message and shows the server reason (confirmation=%s)", async confirmation => {
  pendingConfirmation = confirmation; refuse = "Draft changes are disabled by this server";
  await show(); await send("Keep this requirement");
  expect(host.textContent).toContain(confirmation ? "Operator confirmation required" : refuse);
  expect(input().value).toBe("Keep this requirement");
  expect(input().disabled).toBe(false);
  expect(host.querySelector('[aria-label="需求草稿"]')?.textContent).not.toContain("Keep this requirement");
  expect(writes).toHaveLength(1);
});

it("unlocks an uncertain save and can confirm it by readback without repeating the write", async () => {
  await show(); failReadback = true; await send("Persist once");
  expect(host.textContent).toContain("Readback offline");
  expect(input().value).toBe("Persist once");
  expect(input().disabled).toBe(false);
  failReadback = false;
  act(() => button("重新读取需求").click()); await settle();
  expect(host.querySelector('[aria-label="需求草稿"]')?.textContent).toContain("Persist once");
  expect(input().value).toBe("");
  expect(writes).toHaveLength(1);
});

it.each(["draft", "init"])("reloads a %s two-tab conflict, keeps unsent text and appends to the latest version on resend", async status => {
  task = { ...task, status, requirement: "Original" };
  await show();
  releaseWrite = async () => {
    releaseWrite = undefined;
    task = { ...task, requirement: "Original\n\nOther tab", requirement_versions: [{ version: 2, text: "Original\n\nOther tab" }] };
  };
  await send("My addition");
  expect(writes[0].body.expected_version).toBe(1);
  expect(input().disabled).toBe(false);
  expect(input().value).toBe("My addition");
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("需求已在其他窗口修改，已显示最新版本");
  expect(host.querySelector('[aria-label="需求草稿"]')?.textContent).toContain("Other tab");
  await send(input().value);
  expect(writes[1].body).toMatchObject({ expected_version: 2, requirement: "Original\n\nOther tab\n\nMy addition" });
  expect(task.requirement).toBe("Original\n\nOther tab\n\nMy addition");
  expect(input().value).toBe("");
});

it.each(["draft", "init"])("enables the %s requirement composer and explains the separate Start action", async status => {
  task = { ...task, status, requirement: "Saved requirement", requirement_versions: [{ version: 4, text: "Saved requirement" }] };
  await show();
  expect(input().disabled).toBe(false);
  expect(taskConversationHint("zh", "before_start")).toBe("继续发送会追加到需求。点击「开始」后，这个任务的规划者才会启动。");
  expect(taskConversationHint("en", "before_start")).toBe("Further messages append to the requirement. This task's Planner starts only after you click Start.");
  expect(host.textContent).toContain(taskConversationHint("zh", "before_start"));
  expect(host.textContent).not.toContain("Planner");
  const requirement = host.querySelector('[aria-label="需求草稿"]')!;
  expect(requirement.textContent).toContain("Saved requirement");
  expect(requirement.textContent).toContain("版本 4");
  expect(host.querySelector('[data-work-detail]')?.textContent).not.toContain("任务已结束");
  expect(writes).toHaveLength(0);
});

it.each(["pending", "assigned", "running", "planning", "executing", "verifying", "stopping", "waiting", "waiting_user", "awaiting_budget"])("enables the %s composer only when its Planner is available", async status => {
  task = { ...task, status };
  await show();
  expect(input().disabled).toBe(false);
  expect(host.textContent).toContain(taskConversationHint("zh", "ready"));
  expect(host.textContent).toContain("发送消息与这个任务的规划者对话。");
  expect(host.querySelector('[data-work-detail]')?.textContent).not.toContain("任务已结束");
  act(() => root.unmount()); root = createRoot(host);
  plannerAvailable = false;
  await show();
  expect(input().disabled).toBe(true);
  expect(taskConversationHint("zh", "unavailable")).toBe("这个任务的规划者当前不可用，暂时无法接收消息。");
  const unavailable = host.querySelector("[data-work-detail]")!;
  expect(unavailable.textContent).toContain(taskConversationHint("zh", "unavailable"));
  expect(unavailable.textContent).not.toContain("任务管家");
  expect(unavailable.textContent).not.toContain("Planner");
  expect(host.querySelector('[data-work-detail]')?.textContent).not.toContain("任务已结束");
});

it.each(["completed", "failed", "cancelled", "stopped"])("disables the %s composer with an ended hint", async status => {
  task = { ...task, status };
  await show();
  expect(input().disabled).toBe(true);
  expect(taskConversationHint("zh", "ended")).toBe("任务已结束，这个任务的规划者不再接收消息。");
  const ended = host.querySelector("[data-work-detail]")!;
  expect(ended.textContent).toContain(taskConversationHint("zh", "ended"));
  expect(ended.textContent).not.toContain("任务管家");
  expect(ended.textContent).not.toContain("Planner");
});

it.each(["handing_back", "unknown"])("does not describe unavailable %s as ended", async status => {
  task = { ...task, status };
  await show();
  expect(input().disabled).toBe(true);
  const state = status === "handing_back" ? "handing_back" : "unavailable";
  expect(taskConversationHint("zh", state)).toBe(status === "handing_back"
    ? "任务正在交还，这个任务的规划者暂时不接收消息。"
    : "这个任务的规划者当前不可用，暂时无法接收消息。");
  const detail = host.querySelector("[data-work-detail]")!;
  expect(detail.textContent).toContain(taskConversationHint("zh", state));
  expect(detail.textContent).not.toContain("任务管家");
  expect(detail.textContent).not.toContain("Planner");
  expect(host.querySelector('[data-work-detail]')?.textContent).not.toContain("任务已结束");
});

it("unlocks and keeps input when a successful write is replaced before readback", async () => {
  await show();
  const fetch = globalThis.fetch;
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    const response = await fetch(path, init);
    if (String(path).includes("requirement_update"))
      task = { ...task, requirement: "Replaced elsewhere", requirement_versions: [{ version: 3, text: "Replaced elsewhere" }] };
    return response;
  }));
  await send("Keep this text");
  expect(input().disabled).toBe(false);
  expect(input().value).toBe("Keep this text");
  expect(host.textContent).toContain("需求已在其他窗口修改");
  expect(host.querySelector('[aria-label="需求草稿"]')?.textContent).toContain("Replaced elsewhere");
});

it("shows the identical timestamped untitled name in the list and header, without the internal id", async () => {
  await show();
  const heading = host.querySelector('[aria-label="当前任务"] h1')!.textContent!;
  expect(heading).toContain("未命名任务");
  expect(heading).toMatch(/\d/);
  expect(heading).not.toContain("task-draft");
  const row = host.querySelector('a[href="#work/task/task-draft"]')!;
  expect(row.querySelector('[data-task-title]')?.textContent).toBe(heading);
  expect(row.textContent).not.toContain("task-draft");
});

it("reuses Start and reports its server refusal next to the requirement", async () => {
  task = { ...task, requirement: "Ready to plan" }; refuse = "Planner start refused by server";
  await show();
  act(() => button("开始").click()); await settle();
  expect(writes.some(write => write.url.includes("task_control"))).toBe(true);
  expect(host.textContent).toContain(refuse);
  expect(task.status).toBe("draft");
});

it.each(["draft", "init"])("confirms %s Start with server readback without remounting the control owner", async status => {
  task = { ...task, status, requirement: "Ready to plan" };
  await show();
  act(() => button("开始").click()); await settle();
  expect(task.status).toBe("running");
  expect(host.textContent).toContain("操作已确认");
  expect(host.querySelector('[aria-label="需求草稿"]')).toBeNull();
  expect(writes).toHaveLength(1);
});

it.each(["draft", "init"])("appends to a fresh %s requirement and blocks double-send and Start while saving", async status => {
  task = { ...task, status, requirement: "Initial requirement" };
  await show();
  task = { ...task, requirement: "Edited in the details panel" };
  let finish!: () => void;
  releaseWrite = () => new Promise<void>(resolve => { finish = resolve; });
  await send("Append once");
  expect(button("开始").disabled).toBe(true);
  act(() => {
    input().dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    button("开始").click();
  });
  expect(writes).toHaveLength(1);
  expect(writes[0].body.requirement).toBe("Edited in the details panel\n\nAppend once");
  act(() => finish()); await settle();
  expect(input().value).toBe("");
  expect(button("开始").disabled).toBe(false);
});

it.each([false, true])("restores composer focus after saving unless the user moved it (moved=%s)", async moved => {
  await show();
  let finish!: () => void;
  releaseWrite = () => new Promise<void>(resolve => { finish = resolve; });
  act(() => input().focus());
  await send("Continue typing after save");
  // Browsers blur a focused input when it becomes disabled; emulate that in the DOM test.
  act(() => { input().disabled = false; input().blur(); input().disabled = true; });
  expect(document.activeElement).toBe(document.body);
  const target = moved ? button("新建任务") : input();
  if (moved) act(() => target.focus());
  act(() => finish()); await settle();
  expect(document.activeElement).toBe(target);
});


it("shows a neutral changes placeholder for a draft without reading its workspace", async () => {
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({ matches: query.includes("1280"), addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  await show();
  const changes = host.querySelector('[aria-label="改动文件"]')!;
  expect(changes.textContent).toContain("任务开始后显示改动");
  expect(changes.querySelector('[role="alert"]')).toBeNull();
  expect(host.querySelector('[aria-label="任务管家模型"]')).toBeNull();
});
