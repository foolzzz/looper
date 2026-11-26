// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { WorkPage } from "@/pages/work";

vi.mock("@/lib/use-projects", () => ({ useProjects: () => ({ projects: [{ project_id: "p", name: "Project" }] }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SONNET = "agent-claude-code-sonnet-5-5";
const OPUS = "agent-claude-code-opus";
const BUSY = "the task Manager is working in this task: stop its turn first";
const INVALID = "unknown profile";
const RUNTIME = "Profile codex/opus: the task Manager runs only on Claude Code for now.";

const ZH = {
  heading: "任务管家模型",
  profile: "配置",
  model: "模型",
  runtime: "运行时",
  provider: "提供商",
  source: "来源",
  builtin: "内置默认",
  task: "本任务",
  saving: "正在切换…",
  saved: "已按读回更新",
  busy: "请先停止当前回复再切换模型",
  uncertain: "切换结果尚未确认",
  retry: "重新读取",
  requirement: "Ship the picker",
};
const EN = {
  sonnetModel: "claude-sonnet-5-5",
  opusModel: "opus",
  runtime: "claude-code",
  provider: "prov-claude-subscription",
  defaultSource: "default",
  taskSource: "task",
  sonnetLabel: "claude-code/sonnet-5-5",
  opusLabel: "claude-code/opus",
};

function view(override: string | null, effectiveId: string, model: string, source: string) {
  return {
    task_id: "task-draft",
    override_profile_id: override,
    effective: {
      profile_id: effectiveId,
      model,
      runtime: EN.runtime,
      provider: EN.provider,
      effort: null,
    },
    source,
    choices: [
      { profile_id: SONNET, label: EN.sonnetLabel, model: EN.sonnetModel, runtime: EN.runtime },
      { profile_id: OPUS, label: EN.opusLabel, model: EN.opusModel, runtime: EN.runtime },
    ],
  };
}

const DEFAULT_VIEW = view(null, SONNET, EN.sonnetModel, EN.defaultSource);
const OPUS_VIEW = view(OPUS, OPUS, EN.opusModel, EN.taskSource);

let host: HTMLDivElement;
let root: Root;
let task: Record<string, unknown>;
let profile: unknown;
let profileGetError: Error | null;
let profileSet: Response | Error | "hold" | null;
let releaseSet: ((response: Response) => void) | null;
let generation: number;
const calls: { url: string; method: string; raw: string }[] = [];

const settle = async () => { for (let n = 0; n < 20; n += 1) await act(async () => { await Promise.resolve(); }); };
const section = () => host.querySelector('[aria-label="任务管家模型"]');
const field = (name: string) => section()?.querySelector(`[data-field="${name}"]`)?.textContent ?? "";
const button = (label: string, root: ParentNode = host) =>
  [...root.querySelectorAll("button")].find(item => item.textContent === label);
const posts = (part: string) => calls.filter(call => call.method === "POST" && call.url.includes(part));

async function show(sub = "task/task-draft") {
  generation += 1;
  await act(async () => { root.render(<WorkPage key={generation} sub={sub} onNavigate={() => {}} />); });
  await settle();
}
async function choose(value: string) {
  const input = section()?.querySelector(`input[type="radio"][value="${CSS.escape(value)}"]`) as HTMLInputElement | null;
  expect(input).toBeTruthy();
  await act(async () => { input!.click(); });
  await settle();
}
async function click(label: string) {
  const el = button(label, section() ?? host);
  expect(el).toBeTruthy();
  await act(async () => { (el as HTMLButtonElement).click(); });
  await settle();
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  calls.length = 0;
  generation = 0;
  profile = DEFAULT_VIEW;
  profileGetError = null;
  profileSet = null;
  releaseSet = null;
  task = {
    task_id: "task-draft", title: "", status: "draft", requirement: ZH.requirement, requirement_versions: [],
    created_at: "2026-11-25T10:45:00Z", work_dir: "workspace", project_id: "p", project_name: "Demo",
    steps: [], gates: [], cost: { runs: 0, running: 0 },
  };
  vi.stubGlobal("fetch", vi.fn(async (path: string, init?: RequestInit) => {
    const url = String(path);
    const method = init?.method ?? "GET";
    const raw = typeof init?.body === "string" ? init.body : "";
    calls.push({ url, method, raw });
    if (method === "GET") {
      return new Response(JSON.stringify({ status: "error", error: { code: "invalid", message: "use POST" } }), { status: 405 });
    }
    if (url.includes("task_manager_profile_get")) {
      if (profileGetError) throw profileGetError;
      return Response.json({ status: "ok", result: profile });
    }
    if (url.includes("task_manager_profile_set")) {
      if (profileSet instanceof Error) throw profileSet;
      if (profileSet === "hold") return new Promise<Response>(resolve => { releaseSet = resolve; });
      if (profileSet) return profileSet;
      return Response.json({ status: "ok", result: OPUS_VIEW });
    }
    let result: unknown = {};
    if (url.includes("requirement_update")) result = { task_id: task.task_id, version: 2, requirement: task.requirement };
    else if (url.includes("task_control/start")) {
      task = { ...task, status: "running" };
      result = { task_id: task.task_id, started: true };
    } else if (url.includes("tasks/detail")) result = task;
    else if (url.includes("work_list")) result = { items: [], schedules: [], next_cursor: null };
    else if (url.includes("tasks/activity")) result = { activity: [] };
    else if (url.includes("chat/targets")) result = { targets: [] };
    else if (url.includes("chat/messages")) result = { messages: [] };
    else if (url.includes("create_draft")) result = { task_id: "task-draft", project_id: "p" };
    else if (url.includes("task_conversations")) result = { enabled: true };
    return Response.json({ status: "ok", result });
  }));
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("shows the draft's effective profile and choices in Chinese and English, then the profile read back after Opus", async () => {
  profileSet = "hold";
  await show();
  const model = section();
  expect(model).not.toBeNull();
  expect(model!.textContent).toContain(ZH.heading);
  expect(model!.textContent).toContain(ZH.profile);
  expect(model!.textContent).toContain(ZH.model);
  expect(model!.textContent).toContain(ZH.runtime);
  expect(model!.textContent).toContain(ZH.provider);
  expect(model!.textContent).toContain(ZH.source);
  expect(model!.textContent).toContain(`${EN.defaultSource}（${ZH.builtin}）`);
  expect(model!.textContent).toContain(EN.sonnetLabel);
  expect(model!.textContent).toContain(EN.opusLabel);
  expect(field("profile-id")).toBe(SONNET);
  expect(field("model")).toBe(EN.sonnetModel);
  expect(field("runtime")).toBe(EN.runtime);
  expect(field("provider")).toBe(EN.provider);
  expect(field("source")).toContain(EN.defaultSource);
  expect(field("source")).toContain(ZH.builtin);
  expect(posts("task_manager_profile_set")).toHaveLength(0);

  await choose(OPUS);
  expect(field("model")).toBe(EN.sonnetModel);
  await click("切换模型");
  expect(posts("task_manager_profile_set")).toHaveLength(1);
  expect(posts("task_manager_profile_set")[0].raw).toBe(JSON.stringify({ task_id: "task-draft", profile_id: OPUS }));
  expect(section()!.textContent).toContain(ZH.saving);
  expect(section()!.textContent).toContain(OPUS);
  expect(posts("task_control")).toHaveLength(0);
  expect(posts("requirement_update")).toHaveLength(0);
  expect(posts("chat/send")).toHaveLength(0);
  expect(task.requirement).toBe(ZH.requirement);

  profile = OPUS_VIEW;
  await act(async () => { releaseSet?.(Response.json({ status: "ok", result: OPUS_VIEW })); });
  await settle();
  expect(field("profile-id")).toBe(OPUS);
  expect(field("model")).toBe(EN.opusModel);
  expect(field("source")).toContain(EN.taskSource);
  expect(field("source")).toContain(ZH.task);
  expect(section()!.textContent).toContain(ZH.saved);
  expect(host.textContent).toContain(ZH.requirement);
  expect(posts("task_manager_profile_set")).toHaveLength(1);
  expect(posts("requirement_update")).toHaveLength(0);
  expect(posts("chat/send")).toHaveLength(0);

  const start = button("开始");
  expect(start).toBeTruthy();
  expect((start as HTMLButtonElement).disabled).toBe(false);
  await act(async () => { (start as HTMLButtonElement).click(); });
  await settle();
  expect(posts("task_control/start").map(call => call.raw)).toEqual([JSON.stringify({ task_id: "task-draft" })]);
  expect(posts("task_manager_profile_set")).toHaveLength(1);
  expect(posts("requirement_update")).toHaveLength(0);
  expect(posts("chat/send")).toHaveLength(0);
});

it.each([
  ["busy", 409, "task_manager_busy", BUSY, ZH.busy],
  ["invalid", 400, "invalid", INVALID, INVALID],
  ["runtime", 400, "task_manager_runtime_unsupported", RUNTIME, RUNTIME],
] as const)("keeps the previous profile when %s is refused and shows both languages", async (_name, status, code, message, visible) => {
  profileSet = Response.json({ status: "error", error: { code, message } }, { status });
  await show();
  await choose(OPUS);
  await click("切换模型");
  expect(section()!.textContent).toContain(visible);
  expect(section()!.textContent).toContain(message);
  expect(section()!.textContent).toContain(ZH.heading);
  expect(field("model")).toBe(EN.sonnetModel);
  expect(field("profile-id")).toBe(SONNET);
  expect(field("source")).toContain(EN.defaultSource);
  expect(section()!.textContent).not.toContain(ZH.saved);
  expect(host.textContent).toContain(ZH.requirement);
  expect(task.requirement).toBe(ZH.requirement);
  expect(posts("task_control")).toHaveLength(0);
  expect(posts("requirement_update")).toHaveLength(0);
  expect(posts("chat/send")).toHaveLength(0);
  expect(posts("task_manager_profile_set")).toHaveLength(1);
});

it("shows a retry after a network error and does not send the switch again", async () => {
  profileGetError = new TypeError("network down");
  await show();
  expect(section()!.textContent).toContain(ZH.heading);
  expect(section()!.textContent).toContain("network down");
  expect(button(ZH.retry, section()!)).toBeTruthy();
  expect(posts("task_manager_profile_set")).toHaveLength(0);

  profileGetError = null;
  await click(ZH.retry);
  expect(field("model")).toBe(EN.sonnetModel);
  expect(field("source")).toContain(`${EN.defaultSource}（${ZH.builtin}）`);
  expect(posts("task_manager_profile_set")).toHaveLength(0);

  profileSet = new TypeError("network down");
  await choose(OPUS);
  await click("切换模型");
  expect(section()!.textContent).toContain(ZH.uncertain);
  expect(section()!.textContent).toContain(ZH.retry);
  expect(field("model")).toBe(EN.sonnetModel);
  expect(posts("task_manager_profile_set")).toHaveLength(1);
  await click(ZH.retry);
  expect(posts("task_manager_profile_set")).toHaveLength(1);
  expect(posts("task_control")).toHaveLength(0);
  expect(posts("chat/send")).toHaveLength(0);
  expect(task.requirement).toBe(ZH.requirement);
});

it("creates a draft with the same request body as before", async () => {
  await show("");
  const create = button("新建任务");
  expect(create).toBeTruthy();
  expect((create as HTMLButtonElement).disabled).toBe(false);
  await act(async () => { (create as HTMLButtonElement).click(); });
  await settle();
  expect(posts("create_draft").map(call => call.raw)).toEqual([JSON.stringify({ project_id: "p" })]);
  expect(posts("create_draft")[0].raw).not.toContain("task_manager_profile_id");
  expect(posts("task_manager_profile")).toHaveLength(0);
});
