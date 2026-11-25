// @vitest-environment happy-dom
import { act, useState, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkTaskManagerModel } from "./work-task-manager-model";
import { WorkPage } from "@/pages/work";
import type { TaskDetail, UserGate } from "@/lib/api";
import {
  resetTaskManagerProfileProbe,
  TASK_MANAGER_PROFILE_READ_PATH,
  TASK_MANAGER_PROFILE_SET_PATH,
} from "@/lib/task-manager-profile";

const api = vi.hoisted(() => ({
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

/** A real Claude resume was not approved for this change. */
const REAL_CLAUDE_RESUME_RUN = false;

const SONNET = {
  profile_id: "agent-claude-code-sonnet-5-5",
  name: "claude-code/sonnet-5-5",
  model: "claude-sonnet-5-5",
  runtime: "claude-code",
  provider: "claude-subscription",
  source: "builtin",
};
const OPUS = {
  profile_id: "agent-claude-code-opus",
  name: "claude-code/opus",
  model: "opus",
  runtime: "claude-code",
  provider: "claude-subscription",
  source: "role",
};
const CUSTOM = {
  profile_id: "agent-custom-source",
  name: "custom/profile",
  model: "custom-model",
  runtime: "claude-code",
  provider: "claude-subscription",
  source: "custom-source",
};

type Choice = typeof SONNET & { problem?: string | null; compatible?: boolean };
type View = {
  choices: Choice[];
  effective: (typeof SONNET) | null;
  inherit: boolean;
  selected_profile_id: string | null;
  resume?: Record<string, unknown>;
};

const resumeKept = {
  session_id: "native-session",
  resumed: true,
  history: "history-kept",
  cost_usd: 1.5,
  background_roles: { planner: "profile-planner", worker: "profile-worker", reviewer: "profile-reviewer" },
};

function view(extra: Partial<View> = {}): View {
  return {
    choices: [{ ...SONNET }, { ...OPUS }, { ...CUSTOM }],
    effective: { ...SONNET },
    inherit: true,
    selected_profile_id: SONNET.profile_id,
    resume: resumeKept,
    ...extra,
  };
}

const FORBIDDEN = [
  "manager_session",
  "/api/v1/roles/update",
  "/api/v1/tasks/configure",
  "/api/v1/tasks/agents",
  "agent_settings",
  "project_agent_settings",
  "profile_overrides",
  "looper_task_profile_set",
];

let host: HTMLDivElement;
let root: Root;
const calls: { url: string; body: Record<string, unknown> }[] = [];
let handle: (url: string, body: Record<string, unknown>) => Promise<Response> | Response = () =>
  Response.json({ status: "ok", result: view() });

const ok = (result: unknown) => Response.json({ status: "ok", result });
const fail = (status: number, code: string, message: string, details?: Record<string, unknown>) =>
  Response.json({ status: "error", error: { code, message, details } }, { status });
const field = (name: string) => host.querySelector(`[data-field="${name}"]`)?.textContent ?? "";
const choice = (id: string) => host.querySelector(`[data-choice="${id}"]`) as HTMLElement;
const button = (text: string) => [...host.querySelectorAll("button")].find((el) => el.textContent === text);
const sets = () => calls.filter((call) => call.url.includes(TASK_MANAGER_PROFILE_SET_PATH));
const reads = () => calls.filter((call) => call.url.includes(TASK_MANAGER_PROFILE_READ_PATH));

async function settle() {
  for (let i = 0; i < 10; i += 1) await act(async () => { await Promise.resolve(); });
}
async function show(node: ReactNode) {
  await act(async () => { root.render(node); });
  await settle();
}
async function click(text: string) {
  const el = button(text);
  expect(el).toBeTruthy();
  await act(async () => { (el as HTMLButtonElement).click(); });
  await settle();
}
async function choose(value: string) {
  const el = host.querySelector(`input[type="radio"][value="${CSS.escape(value)}"]`) as HTMLInputElement;
  expect(el).toBeTruthy();
  await act(async () => { el.click(); });
  await settle();
}

beforeEach(() => {
  resetTaskManagerProfileProbe();
  calls.length = 0;
  handle = () => ok(view());
  vi.resetAllMocks();
  api.fetchTaskDetail.mockResolvedValue({ task_id: "A", title: "Task A", status: "running", group: "in_progress", gates: [] });
  api.fetchTaskActivityWindow.mockResolvedValue({ events: [], hasMore: false });
  api.fetchGateThread.mockResolvedValue({ messages: [] });
  api.fetchChatMessages.mockResolvedValue([]);
  api.fetchChatTargets.mockResolvedValue([]);
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
    calls.push({ url: String(url), body });
    return handle(String(url), body);
  }));
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("task manager model choices", () => {
  it("shows Sonnet and Opus by server id, source, model, runtime and provider", async () => {
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(host.textContent).toContain("任务管家模型");
    for (const profile of [SONNET, OPUS]) {
      const row = choice(profile.profile_id);
      expect(row.textContent).toContain(profile.profile_id);
      expect(row.textContent).toContain(profile.model);
      expect(row.textContent).toContain(profile.runtime);
      expect(row.textContent).toContain(profile.provider);
      expect(row.textContent).toContain(profile.source);
      expect((row.querySelector("input") as HTMLInputElement).disabled).toBe(false);
    }
    expect(choice(SONNET.profile_id).textContent).toContain("内置默认");
    expect(choice(OPUS.profile_id).textContent).toContain("角色配置");
    expect(choice(CUSTOM.profile_id).textContent).toContain("custom-source");
    expect(choice(CUSTOM.profile_id).textContent).not.toContain("内置默认");
    expect(field("profile-id")).toBe(SONNET.profile_id);
    expect(field("model")).toBe(SONNET.model);
    expect(field("runtime")).toBe(SONNET.runtime);
    expect(field("provider")).toBe(SONNET.provider);
    expect(field("source")).toContain("builtin");
    expect(field("source")).toContain("内置默认");
    expect(choice("inherit")).not.toBeNull();
    expect(reads()[0]?.body).toEqual({ task_id: "A" });
    expect(sets()).toHaveLength(0);
  });

  it("offers inherit only when the read says the upstream schema supports null", async () => {
    handle = () => ok(view({ inherit: false }));
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(choice("inherit")).toBeNull();
    handle = async () => ok({ choices: [SONNET, OPUS], effective: SONNET, selected_profile_id: null });
    await show(<WorkTaskManagerModel mode="task" taskId="B" />);
    expect(host.textContent).toContain("模型读取失败");
    expect(choice("inherit")).toBeNull();
  });

  it("keeps a non-Claude runtime selectable until the server marks it incompatible", async () => {
    const codex = { ...OPUS, profile_id: "agent-codex-opus", runtime: "codex", model: "codex-opus" };
    handle = () => ok(view({ choices: [{ ...SONNET }, codex] }));
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect((choice(codex.profile_id).querySelector("input") as HTMLInputElement).disabled).toBe(false);
  });
});

describe("task manager model writes", () => {
  it("does not post twice while a switch is in flight, then shows the readback", async () => {
    let release: ((response: Response) => void) | null = null;
    let current = view();
    handle = (url) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) {
        return new Promise((resolve) => {
          release = resolve;
        });
      }
      return ok(current);
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    await choose(OPUS.profile_id);
    const save = button("切换模型") as HTMLButtonElement;
    await act(async () => { save.click(); save.click(); });
    expect(sets()).toHaveLength(1);
    expect(sets()[0]?.body).toEqual({ task_id: "A", profile_id: OPUS.profile_id });
    expect(host.textContent).toContain("正在切换…");
    expect(host.textContent).not.toContain("已按读回更新");
    current = view({
      effective: { ...OPUS, source: "task" },
      selected_profile_id: OPUS.profile_id,
    });
    await act(async () => {
      release?.(ok({ task_id: "A", profile_id: OPUS.profile_id }));
    });
    await settle();
    expect(sets()).toHaveLength(1);
    expect(field("profile-id")).toBe(OPUS.profile_id);
    expect(field("model")).toBe(OPUS.model);
    expect(field("runtime")).toBe(OPUS.runtime);
    expect(field("provider")).toBe(OPUS.provider);
    expect(field("source")).toContain("task");
    expect(field("source")).toContain("本任务");
    expect(host.textContent).toContain("已按读回更新");
    expect(host.textContent).toContain("读回：会话 native-session 续接");
    expect(host.textContent).not.toContain("原会话已续上");
  });

  it("keeps the previous effective value for busy, runtime, invalid, failure and an uncertain write", async () => {
    const cases: { name: string; response: Response | "network"; text: string; absent?: string }[] = [
      { name: "busy", response: fail(409, "conflict", "the task Manager is working in this task: stop its turn first"), text: "请先停止当前回复再切换模型" },
      { name: "busy-code", response: fail(409, "task_manager_busy", "stop its turn first"), text: "请先停止当前回复再切换模型" },
      {
        name: "runtime",
        response: fail(400, "invalid", "Profile codex/opus: the task Manager runs only on Claude Code for now.", { requested: "codex", supported: "claude_code" }),
        text: "requested runtime codex",
      },
      {
        name: "runtime-plain",
        response: fail(400, "invalid", "Profile other: the task Manager runs only on Claude Code for now."),
        text: "runs only on Claude Code",
        absent: "claude_code",
      },
      { name: "invalid", response: fail(400, "invalid", "unknown profile"), text: "unknown profile" },
      { name: "failure", response: fail(500, "unavailable", "write failed"), text: "切换失败" },
      { name: "work-mode", response: fail(409, "conflict", "work mode is off (tasks.conversations.enabled)"), text: "work mode is off", absent: "请先停止当前回复再切换模型" },
      { name: "network", response: "network", text: "切换结果尚未确认" },
      { name: "shape", response: ok({ task_id: "A", profile_id: OPUS.profile_id, model: OPUS.model }), text: "切换结果尚未确认" },
      { name: "confirm", response: Response.json({ status: "needs_confirmation", confirmation_id: "c1", expires_in_seconds: 30, impact: "switch" }, { status: 202 }), text: "切换结果尚未确认" },
    ];
    for (const item of cases) {
      resetTaskManagerProfileProbe();
      calls.length = 0;
      handle = (url) => {
        if (!url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok(view());
        if (item.response === "network") throw new TypeError("network down");
        return item.response;
      };
      await show(<WorkTaskManagerModel mode="task" taskId={`task-${item.name}`} />);
      await choose(OPUS.profile_id);
      await click("切换模型");
      expect(host.textContent, item.name).toContain(item.text);
      if (item.name === "runtime") expect(host.textContent).toContain("supported runtime claude_code");
      if (item.absent) expect(host.textContent, item.name).not.toContain(item.absent);
      expect(host.textContent, item.name).not.toContain("已按读回更新");
      expect(field("model"), item.name).toBe(SONNET.model);
      expect(field("profile-id"), item.name).toBe(SONNET.profile_id);
      expect((choice(OPUS.profile_id).querySelector("input") as HTMLInputElement).checked, item.name).toBe(true);
      expect(calls.some((call) => call.url.includes("/api/v1/confirm")), item.name).toBe(false);
    }
  });

  it("sends exactly one set when the switch is used again after an unknown result", async () => {
    handle = (url) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) throw new TypeError("network down");
      return ok(view());
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(sets()).toHaveLength(1);
    const save = button("切换模型") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    const radios = [...host.querySelectorAll("input[type='radio']")] as HTMLInputElement[];
    expect(radios.length).toBeGreaterThan(0);
    expect(radios.every((input) => input.disabled)).toBe(true);
    expect((button("重新读取") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { save.click(); });
    await settle();
    expect(sets()).toHaveLength(1);
  });

  it("disables writes after an unknown result or a failed readback and offers only re-read", async () => {
    handle = (url) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) throw new TypeError("network down");
      return ok(view());
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(host.textContent).toContain("切换结果尚未确认");
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(true);
    expect(([...host.querySelectorAll("input[type='radio']")] as HTMLInputElement[]).every((input) => input.disabled)).toBe(true);
    expect((button("重新读取") as HTMLButtonElement).disabled).toBe(false);

    calls.length = 0;
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok({ task_id: body.task_id, profile_id: body.profile_id });
      if (sets().length) return fail(500, "unavailable", "readback down");
      return ok(view());
    };
    await show(<WorkTaskManagerModel mode="task" taskId="readback" />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(host.textContent).toContain("模型读取失败");
    const save = button("切换模型") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(([...host.querySelectorAll("input[type='radio']")] as HTMLInputElement[]).every((input) => input.disabled)).toBe(true);
    expect((button("重新读取") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { save.click(); });
    await settle();
    expect(sets()).toHaveLength(1);
  });

  it("retries an uncertain write by reading, without posting again", async () => {
    let writes = 0;
    handle = (url) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) {
        writes += 1;
        throw new TypeError("network down");
      }
      return ok(view());
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(host.textContent).toContain("切换结果尚未确认");
    expect(writes).toBe(1);
    await click("重新读取");
    expect(writes).toBe(1);
    expect(field("model")).toBe(SONNET.model);
    expect((choice(OPUS.profile_id).querySelector("input") as HTMLInputElement).checked).toBe(true);
    expect(host.textContent).not.toContain("已按读回更新");
  });

  it("keeps the old effective profile when the readback does not match", async () => {
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok({ task_id: body.task_id, profile_id: body.profile_id });
      return ok(view());
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(host.textContent).toContain(`读回的 Profile 是 ${SONNET.profile_id}`);
    expect(host.textContent).not.toContain("已按读回更新");
    expect(field("profile-id")).toBe(SONNET.profile_id);
    expect((choice(OPUS.profile_id).querySelector("input") as HTMLInputElement).checked).toBe(true);
    expect(sets()).toHaveLength(1);
  });

  it("treats a null profile id as the role default only after the readback says so", async () => {
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) {
        expect(body).toEqual({ task_id: "A", profile_id: null });
        return ok({ task_id: "A", profile_id: null });
      }
      if (sets().length) return ok(view({ selected_profile_id: null, effective: { ...SONNET, source: "role" } }));
      return ok(view());
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    await choose("");
    await click("切换模型");
    expect(field("profile-id")).toBe(SONNET.profile_id);
    expect(field("source")).toContain("角色配置");
    expect(host.textContent).toContain("已按读回更新");
    expect((choice("inherit").querySelector("input") as HTMLInputElement).checked).toBe(true);
  });
});

describe("task manager model availability", () => {
  it("shows loading, then a read failure with a retry, and does not poll", async () => {
    let release: ((response: Response) => void) | null = null;
    handle = () => new Promise((resolve) => { release = resolve; });
    await act(async () => { root.render(<WorkTaskManagerModel mode="task" taskId="A" />); });
    expect(host.textContent).toContain("正在读取任务管家模型");
    await act(async () => { release?.(fail(500, "unavailable", "db down")); });
    await settle();
    expect(host.textContent).toContain("模型读取失败");
    expect(button("重试")).toBeTruthy();
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(reads()).toHaveLength(1);
    vi.useRealTimers();
    handle = () => ok(view());
    await click("重试");
    expect(field("model")).toBe(SONNET.model);
    expect(reads()).toHaveLength(2);
  });

  it("shows the unavailable state for an unknown route and does not request it again", async () => {
    handle = (url) => fail(404, "not_found", `no post ${url}`);
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(host.textContent).toContain("此服务器尚不支持任务模型切换");
    expect(button("重试")).toBeUndefined();
    expect(button("切换模型")).toBeUndefined();
    const first = reads().length;
    await show(<WorkTaskManagerModel mode="task" taskId="B" />);
    expect(reads()).toHaveLength(first);
    expect(host.textContent).toContain("此服务器尚不支持任务模型切换");
  });

  it("does not treat a missing task as a missing operation", async () => {
    let taskReads = 0;
    handle = () => {
      taskReads += 1;
      return fail(404, "not_found", "task not found");
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(host.textContent).toContain("模型读取失败");
    expect(host.textContent).not.toContain("此服务器尚不支持任务模型切换");
    await click("重试");
    expect(taskReads).toBe(2);
  });

  it("disables switching when there are no choices", async () => {
    handle = () => ok(view({ choices: [], effective: { ...SONNET } }));
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(host.textContent).toContain("没有可用的任务管家模型");
    expect(host.querySelector("input[type='radio']")).toBeNull();
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(true);
    expect(field("model")).toBe(SONNET.model);
  });

  it("disables an incompatible profile and keeps its server reason", async () => {
    handle = () => ok(view({
      choices: [
        { ...SONNET },
        { ...OPUS, compatible: false, problem: "runtime is not claude-code" },
      ],
    }));
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    const opus = choice(OPUS.profile_id);
    expect((opus.querySelector("input") as HTMLInputElement).disabled).toBe(true);
    expect(opus.textContent).toContain("runtime is not claude-code");
    expect((choice(SONNET.profile_id).querySelector("input") as HTMLInputElement).disabled).toBe(false);
    await act(async () => { (opus.querySelector("input") as HTMLInputElement).click(); });
    expect((choice(SONNET.profile_id).querySelector("input") as HTMLInputElement).checked).toBe(true);
  });

  it("shows the operator selection when it differs from the effective profile", async () => {
    handle = () => ok(view({ selected_profile_id: null }));
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(host.textContent).toContain("当前选择与有效配置不同");
    expect(host.textContent).toContain("跟随角色默认");
    expect(field("profile-id")).toBe(SONNET.profile_id);
    await choose(OPUS.profile_id);
    expect(host.textContent).toContain(OPUS.profile_id);
    expect(field("profile-id")).toBe(SONNET.profile_id);
  });

  it("drops a late response from task A after the operator switches to task B", async () => {
    let releaseA: ((response: Response) => void) | null = null;
    handle = (url, body) => {
      void url;
      if (body.task_id === "A") return new Promise((resolve) => { releaseA = resolve; });
      return ok(view({
        effective: { ...OPUS, source: "task" },
        selected_profile_id: OPUS.profile_id,
      }));
    };
    await act(async () => { root.render(<WorkTaskManagerModel mode="task" taskId="A" />); });
    await show(<WorkTaskManagerModel mode="task" taskId="B" />);
    expect(field("profile-id")).toBe(OPUS.profile_id);
    await act(async () => {
      releaseA?.(ok(view({ effective: { ...SONNET, model: "late-sonnet" }, selected_profile_id: SONNET.profile_id })));
    });
    await settle();
    expect(field("model")).toBe(OPUS.model);
    expect(host.textContent).not.toContain("late-sonnet");
  });

  it("drops a late write for task A after switching to task B", async () => {
    let releaseA: ((response: Response) => void) | null = null;
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH) && body.task_id === "A") {
        return new Promise((resolve) => { releaseA = resolve; });
      }
      if (body.task_id === "B") {
        return ok(view({ effective: { ...CUSTOM, source: "task" }, selected_profile_id: CUSTOM.profile_id }));
      }
      return ok(view());
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    await choose(OPUS.profile_id);
    await act(async () => { (button("切换模型") as HTMLButtonElement).click(); });
    await show(<WorkTaskManagerModel mode="task" taskId="B" />);
    expect(field("profile-id")).toBe(CUSTOM.profile_id);
    await act(async () => { releaseA?.(ok({ task_id: "A", profile_id: OPUS.profile_id })); });
    await settle();
    expect(field("profile-id")).toBe(CUSTOM.profile_id);
    expect(host.textContent).not.toContain("已按读回更新");
  });
});

describe("draft profile selection", () => {
  it("returns only the profile id and never writes a task or another role", async () => {
    const onChange = vi.fn();
    await show(<WorkTaskManagerModel mode="create" onChange={onChange} />);
    expect(onChange).not.toHaveBeenCalled();
    expect(reads()[0]?.body).toEqual({});
    expect(button("切换模型")).toBeUndefined();
    await choose(OPUS.profile_id);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(OPUS.profile_id);
    await choose("");
    expect(onChange).toHaveBeenLastCalledWith(null);
    expect(sets()).toHaveLength(0);
    expect(calls.every((call) => call.url.includes(TASK_MANAGER_PROFILE_READ_PATH))).toBe(true);
    expect(calls.every((call) => FORBIDDEN.every((token) => !call.url.includes(token)))).toBe(true);
  });

  it("emits nothing when the draft read is unavailable", async () => {
    const onChange = vi.fn();
    handle = (url) => fail(404, "not_found", `no post ${url}`);
    await show(<WorkTaskManagerModel mode="create" onChange={onChange} />);
    expect(host.textContent).toContain("此服务器尚不支持任务模型切换");
    expect(onChange).not.toHaveBeenCalled();
    expect(sets()).toHaveLength(0);
  });
});

describe("resume readback", () => {
  it("keeps the same session, history, cost and background roles after a fake switch", async () => {
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok({ task_id: body.task_id, profile_id: body.profile_id });
      return ok(view(sets().length ? {
        effective: { ...OPUS, source: "task" },
        selected_profile_id: OPUS.profile_id,
        resume: resumeKept,
      } : {}));
    };
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(field("session-id")).toBe("native-session");
    expect(field("history")).toBe("history-kept");
    expect(field("cost")).toBe("1.5");
    expect(field("planner")).toBe("profile-planner");
    expect(field("worker")).toBe("profile-worker");
    expect(field("reviewer")).toBe("profile-reviewer");
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(field("session-id")).toBe("native-session");
    expect(field("history")).toBe("history-kept");
    expect(field("cost")).toBe("1.5");
    expect(field("planner")).toBe("profile-planner");
    expect(field("worker")).toBe("profile-worker");
    expect(field("reviewer")).toBe("profile-reviewer");
    expect(host.textContent).toContain("读回：会话 native-session 续接");
    expect(host.textContent).not.toContain("原会话已续上");
    expect(sets()).toHaveLength(1);
    expect(calls.every((call) => FORBIDDEN.every((token) => !call.url.includes(token)))).toBe(true);
  });

  it("shows session_fresh only from an explicit event", async () => {
    handle = () => ok(view({
      resume: { session_id: "native-session", resumed: true, session_fresh: { reason: "resume failed" } },
    }));
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(host.textContent).toContain("原会话续不上，已用摘要重开");
    expect(host.textContent).toContain("resume failed");
    expect(host.textContent).not.toContain("读回：会话 native-session 续接");
    expect(host.textContent).not.toContain("原会话已续上");
  });

  it("says when the server did not state whether the session continued", async () => {
    handle = () => ok(view({ resume: { session_id: "native-session", resumed: false } }));
    await show(<WorkTaskManagerModel mode="task" taskId="A" />);
    expect(host.textContent).toContain("服务端未说明会话是否续接");
    expect(host.textContent).not.toContain("原会话已续上");
    expect(host.textContent).not.toContain("读回：会话 native-session 续接");
  });

  it("does not run a real Claude resume", () => {
    expect(REAL_CLAUDE_RESUME_RUN).toBe(false);
  });
});

describe("Work panel mount", () => {
  const gate = (): UserGate => ({
    step: "decision", step_id: "gate-step", state: "open", topic: "Choose", findings: "", summary: null,
    resolution: "", agenda: [], ended_by_user: false, plan_change: null, depends_on: [], waiting_on: "user",
    not_delivered: 0, retry_at: null, turns: 0, reminders: 0, opened_at: "2026-11-01T10:00:00Z",
    first_reply_at: null, closed_at: null, channel_posts: null,
  });
  function task(id: string): TaskDetail {
    return { task_id: id, title: id, status: "running", group: "in_progress", gates: [gate()] } as TaskDetail;
  }

  it("requests the profile only while the task panel is visible", async () => {
    api.fetchTaskDetail.mockImplementation(async (id: string) => task(id));
    handle = (url) => {
      if (url.includes("task_manager_profile")) return fail(404, "not_found", `no post ${url}`);
      return ok({ activity: [], seq: 0 });
    };
    await show(<WorkPage sub="manager" onNavigate={vi.fn()} />);
    expect(host.textContent).not.toContain("任务管家模型");
    expect(reads()).toHaveLength(0);
    expect(sets()).toHaveLength(0);

    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(reads()).toHaveLength(0);
    await click("任务详情");
    expect(host.textContent).toContain("此服务器尚不支持任务模型切换");
    expect(reads()).toHaveLength(1);
    const opened = reads().length;
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(reads()).toHaveLength(opened);
    vi.useRealTimers();

    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
      matches: String(query).includes("1280"),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    resetTaskManagerProfileProbe();
    calls.length = 0;
    function Harness() {
      const [sub, setSub] = useState("task/A");
      return <WorkPage sub={sub} onNavigate={setSub} />;
    }
    await show(<Harness />);
    expect(host.querySelector('[aria-label="任务管家模型"] h2')?.textContent).toBe("任务管家模型");
    expect(reads()).toHaveLength(1);
    await act(async () => { root.render(<WorkPage sub="task/B" onNavigate={vi.fn()} />); });
    await settle();
    expect(reads()).toHaveLength(1);
    expect(host.textContent).toContain("此服务器尚不支持任务模型切换");
    expect(sets()).toHaveLength(0);
  });
});
