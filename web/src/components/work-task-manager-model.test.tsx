// @vitest-environment happy-dom
import { useState, type ReactNode } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkTaskManagerModel, type TaskManagerSnapshot } from "./work-task-manager-model";
import { WorkPage } from "@/pages/work";
import type { TaskDetail, UserGate } from "@/lib/api";
import { TASK_MANAGER_PROFILE_SET_PATH } from "@/lib/task-manager-profile";

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

function snap(extra: Partial<TaskManagerSnapshot> & { choices?: Choice[] } = {}): TaskManagerSnapshot {
  return {
    choices: [{ ...SONNET }, { ...OPUS }, { ...CUSTOM }],
    effective: { ...SONNET },
    inherit: true,
    selectedProfileId: SONNET.profile_id,
    ...extra,
  };
}

const FORBIDDEN = [
  "task_manager_profile_get",
  "manager_session",
  "/api/v1/roles/update",
  "/api/v1/tasks/configure",
  "/api/v1/tasks/agents",
  "agent_settings",
  "project_agent_settings",
  "profile_overrides",
  "looper_task_profile_set",
];

function TaskBox({
  taskId,
  snapshot,
  onReread,
}: {
  taskId: string;
  snapshot: TaskManagerSnapshot | null;
  onReread: () => Promise<TaskManagerSnapshot>;
}) {
  const [current, setCurrent] = useState(snapshot);
  const [bound, setBound] = useState(taskId);
  if (bound !== taskId) {
    setBound(taskId);
    setCurrent(snapshot);
  }
  return (
    <WorkTaskManagerModel
      mode="task"
      taskId={taskId}
      snapshot={current}
      onReread={async () => {
        const next = await onReread();
        setCurrent(next);
        return next;
      }}
    />
  );
}

let host: HTMLDivElement;
let root: Root;
const calls: { url: string; body: Record<string, unknown> }[] = [];
let handle: (url: string, body: Record<string, unknown>) => Promise<Response> | Response = () =>
  Response.json({ status: "ok", result: {} });

const ok = (result: unknown) => Response.json({ status: "ok", result });
const fail = (status: number, code: string, message: string) =>
  Response.json({ status: "error", error: { code, message } }, { status });
function aborted(): Error {
  const error = new Error("aborted");
  error.name = "AbortError";
  return error;
}
const field = (name: string) => host.querySelector(`[data-field="${name}"]`)?.textContent ?? "";
const choice = (id: string) => host.querySelector(`[data-choice="${id}"]`) as HTMLElement;
const button = (text: string) => [...host.querySelectorAll("button")].find((el) => el.textContent === text);
const sets = () => calls.filter((call) => call.url.includes(TASK_MANAGER_PROFILE_SET_PATH));
const radios = () => [...host.querySelectorAll("input[type='radio']")] as HTMLInputElement[];

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
  calls.length = 0;
  handle = () => ok({});
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
  it("hides the unavailable model section without requesting or polling", async () => {
    await show(<TaskBox taskId="A" snapshot={null} onReread={vi.fn()} />);
    expect(host.querySelector('[aria-label="任务管家模型"]')).toBeNull();
    expect(calls).toHaveLength(0);
  });

  it("shows the profiles passed in, and does not read", async () => {
    await show(<TaskBox taskId="A" snapshot={snap()} onReread={async () => snap()} />);
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
    expect(field("profile-id")).toBe(SONNET.profile_id);
    expect(field("model")).toBe(SONNET.model);
    expect(field("source")).toContain("builtin");
    expect(choice("inherit")).not.toBeNull();
    expect(sets()).toHaveLength(0);
    expect(calls.every((call) => FORBIDDEN.every((token) => !call.url.includes(token)))).toBe(true);
  });

  it("hides inherit unless the snapshot allows null", async () => {
    await show(<TaskBox taskId="A" snapshot={snap({ inherit: false })} onReread={async () => snap()} />);
    expect(choice("inherit")).toBeNull();
  });

  it("keeps a non-Claude runtime selectable until the snapshot marks it incompatible", async () => {
    const codex = { ...OPUS, profile_id: "agent-codex-opus", runtime: "codex", model: "codex-opus" };
    await show(<TaskBox taskId="A" snapshot={snap({ choices: [{ ...SONNET }, codex] })} onReread={async () => snap()} />);
    expect((choice(codex.profile_id).querySelector("input") as HTMLInputElement).disabled).toBe(false);
  });

  it("shows the operator selection when it differs from the effective profile", async () => {
    await show(<TaskBox taskId="A" snapshot={snap({ selectedProfileId: null })} onReread={async () => snap()} />);
    expect(host.textContent).toContain("当前选择与有效配置不同");
    expect(host.textContent).toContain("跟随角色默认");
    expect(field("profile-id")).toBe(SONNET.profile_id);
    await choose(OPUS.profile_id);
    expect(host.textContent).toContain(OPUS.profile_id);
    expect(field("profile-id")).toBe(SONNET.profile_id);
  });

  it("disables an incompatible profile and keeps its server reason", async () => {
    await show(<TaskBox taskId="A" snapshot={snap({
      choices: [{ ...SONNET }, { ...OPUS, compatible: false, problem: "runtime is not claude-code" }],
    })} onReread={async () => snap()} />);
    const opus = choice(OPUS.profile_id);
    expect((opus.querySelector("input") as HTMLInputElement).disabled).toBe(true);
    expect(opus.textContent).toContain("runtime is not claude-code");
    await act(async () => { (opus.querySelector("input") as HTMLInputElement).click(); });
    expect((choice(SONNET.profile_id).querySelector("input") as HTMLInputElement).checked).toBe(true);
  });

  it("disables switching when there are no choices", async () => {
    await show(<TaskBox taskId="A" snapshot={snap({ choices: [] })} onReread={async () => snap()} />);
    expect(host.textContent).toContain("没有可用的任务管家模型");
    expect(host.querySelector("input[type='radio']")).toBeNull();
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(true);
    expect(field("model")).toBe(SONNET.model);
  });
});

describe("task manager model writes", () => {
  it("posts one set while a switch is in flight, then shows the confirmed profile", async () => {
    let release: ((response: Response) => void) | null = null;
    handle = (url) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return new Promise((resolve) => { release = resolve; });
      return ok({});
    };
    const confirmed = snap({
      effective: { ...OPUS, source: "task" },
      selectedProfileId: OPUS.profile_id,
    });
    await show(<TaskBox taskId="A" snapshot={snap()} onReread={async () => confirmed} />);
    await choose(OPUS.profile_id);
    const save = button("切换模型") as HTMLButtonElement;
    await act(async () => { save.click(); save.click(); });
    expect(sets()).toHaveLength(1);
    expect(sets()[0]?.body).toEqual({ task_id: "A", profile_id: OPUS.profile_id });
    expect(host.textContent).toContain("正在切换…");
    expect(host.textContent).not.toContain("已按读回更新");
    await act(async () => { release?.(ok({ task_id: "A", profile_id: OPUS.profile_id })); });
    await settle();
    expect(sets()).toHaveLength(1);
    expect(field("profile-id")).toBe(OPUS.profile_id);
    expect(field("model")).toBe(OPUS.model);
    expect(field("source")).toContain("task");
    expect(field("source")).toContain("本任务");
    expect(host.textContent).toContain("已按读回更新");
    expect(calls.every((call) => FORBIDDEN.every((token) => !call.url.includes(token)))).toBe(true);
  });

  it("keeps the previous effective value for a known refusal", async () => {
    const cases: { name: string; response: Response | "network"; text: string; absent?: string; locked: boolean }[] = [
      { name: "busy", response: fail(409, "conflict", "the task Manager is working in this task: stop its turn first"), text: "请先停止当前回复再切换模型", locked: false },
      { name: "runtime", response: fail(400, "invalid", "Profile codex/opus: the task Manager runs only on Claude Code for now."), text: "runs only on Claude Code", absent: "requested runtime", locked: false },
      { name: "invalid", response: fail(400, "invalid", "unknown profile"), text: "unknown profile", locked: false },
      { name: "failure", response: fail(500, "unavailable", "write failed"), text: "切换失败", locked: false },
      { name: "work-mode", response: fail(409, "conflict", "work mode is off (tasks.conversations.enabled)"), text: "work mode is off", absent: "请先停止当前回复再切换模型", locked: false },
      { name: "network", response: "network", text: "切换结果尚未确认", locked: true },
      { name: "shape", response: ok({ task_id: "A", profile_id: OPUS.profile_id, model: OPUS.model }), text: "切换结果尚未确认", locked: true },
      { name: "confirm", response: Response.json({ status: "needs_confirmation", confirmation_id: "c1", expires_in_seconds: 30, impact: "switch" }, { status: 202 }), text: "切换结果尚未确认", locked: true },
    ];
    for (const item of cases) {
      calls.length = 0;
      handle = (url) => {
        if (!url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok({});
        if (item.response === "network") throw new TypeError("network down");
        return item.response;
      };
      await show(<TaskBox taskId={`task-${item.name}`} snapshot={snap()} onReread={async () => snap()} />);
      await choose(OPUS.profile_id);
      await click("切换模型");
      expect(host.textContent, item.name).toContain(item.text);
      if (item.absent) expect(host.textContent, item.name).not.toContain(item.absent);
      expect(host.textContent, item.name).not.toContain("已按读回更新");
      expect(field("model"), item.name).toBe(SONNET.model);
      expect(field("profile-id"), item.name).toBe(SONNET.profile_id);
      expect((choice(OPUS.profile_id).querySelector("input") as HTMLInputElement).checked, item.name).toBe(true);
      expect((button("切换模型") as HTMLButtonElement).disabled, item.name).toBe(item.locked);
      expect(calls.some((call) => call.url.includes("/api/v1/confirm")), item.name).toBe(false);
    }
  });

  it("sends exactly one set when the switch is used again after an unknown result", async () => {
    handle = (url) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) throw new TypeError("network down");
      return ok({});
    };
    await show(<TaskBox taskId="A" snapshot={snap()} onReread={async () => snap()} />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(sets()).toHaveLength(1);
    const save = button("切换模型") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(radios().length).toBeGreaterThan(0);
    expect(radios().every((input) => input.disabled)).toBe(true);
    expect((button("重新读取") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { save.click(); });
    await settle();
    expect(sets()).toHaveLength(1);
  });

  it("disables writes after an unknown result or a failed readback and offers only re-read", async () => {
    handle = (url) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) throw new TypeError("network down");
      return ok({});
    };
    await show(<TaskBox taskId="unknown" snapshot={snap()} onReread={async () => snap()} />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(host.textContent).toContain("切换结果尚未确认");
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(true);
    expect(radios().every((input) => input.disabled)).toBe(true);
    expect((button("重新读取") as HTMLButtonElement).disabled).toBe(false);

    calls.length = 0;
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok({ task_id: body.task_id, profile_id: body.profile_id });
      return ok({});
    };
    await show(<TaskBox taskId="readback" snapshot={snap()} onReread={async () => { throw new Error("readback down"); }} />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(host.textContent).toContain("模型读取失败");
    const save = button("切换模型") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(radios().every((input) => input.disabled)).toBe(true);
    expect((button("重新读取") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { save.click(); });
    await settle();
    expect(sets()).toHaveLength(1);
    expect(field("profile-id")).toBe(SONNET.profile_id);
  });

  it("locks writes when the set is aborted and a second click does not send another set", async () => {
    handle = (url) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) throw aborted();
      return ok({});
    };
    await show(<TaskBox taskId="abort-set" snapshot={snap()} onReread={async () => snap()} />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(sets()).toHaveLength(1);
    expect(host.textContent).toContain("切换结果尚未确认");
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(true);
    expect(radios().length).toBeGreaterThan(0);
    expect(radios().every((input) => input.disabled)).toBe(true);
    expect((button("重新读取") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { (button("切换模型") as HTMLButtonElement).click(); });
    await settle();
    expect(sets()).toHaveLength(1);
  });

  it("locks writes when the readback is aborted after a successful set and a second click does not send another set", async () => {
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok({ task_id: body.task_id, profile_id: body.profile_id });
      return ok({});
    };
    await show(<TaskBox taskId="abort-readback" snapshot={snap()} onReread={async () => { throw aborted(); }} />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(sets()).toHaveLength(1);
    expect(host.textContent).toContain("模型读取失败");
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(true);
    expect(radios().every((input) => input.disabled)).toBe(true);
    expect((button("重新读取") as HTMLButtonElement).disabled).toBe(false);
    await act(async () => { (button("切换模型") as HTMLButtonElement).click(); });
    await settle();
    expect(sets()).toHaveLength(1);
    expect(field("profile-id")).toBe(SONNET.profile_id);
  });

  it("unlocks after a re-read confirms the server profile", async () => {
    let readback: () => Promise<TaskManagerSnapshot> = async () => { throw new Error("readback down"); };
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok({ task_id: body.task_id, profile_id: body.profile_id });
      return ok({});
    };
    await show(<TaskBox taskId="A" snapshot={snap()} onReread={() => readback()} />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(true);
    const confirmed = snap({
      effective: { ...OPUS, source: "task" },
      selectedProfileId: OPUS.profile_id,
    });
    readback = async () => confirmed;
    await click("重新读取");
    expect(sets()).toHaveLength(1);
    expect(field("profile-id")).toBe(OPUS.profile_id);
    expect(field("model")).toBe(OPUS.model);
    expect(host.textContent).toContain("已按读回更新");
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(false);
    expect(radios().every((input) => input.disabled)).toBe(false);
  });

  it("keeps the old effective profile when the readback does not match", async () => {
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) return ok({ task_id: body.task_id, profile_id: body.profile_id });
      return ok({});
    };
    await show(<TaskBox taskId="A" snapshot={snap()} onReread={async () => snap()} />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(host.textContent).toContain(`读回的配置是 ${SONNET.profile_id}`);
    expect(host.textContent).not.toContain("已按读回更新");
    expect(field("profile-id")).toBe(SONNET.profile_id);
    expect((choice(OPUS.profile_id).querySelector("input") as HTMLInputElement).checked).toBe(true);
    expect((button("切换模型") as HTMLButtonElement).disabled).toBe(false);
    expect(sets()).toHaveLength(1);
  });

  it("treats a null profile id as the role default only after the readback says so", async () => {
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH)) {
        expect(body).toEqual({ task_id: "A", profile_id: null });
        return ok({ task_id: "A", profile_id: null });
      }
      return ok({});
    };
    const confirmed = snap({ selectedProfileId: null, effective: { ...SONNET, source: "role" } });
    await show(<TaskBox taskId="A" snapshot={snap()} onReread={async () => confirmed} />);
    await choose("");
    await click("切换模型");
    expect(field("profile-id")).toBe(SONNET.profile_id);
    expect(field("source")).toContain("角色配置");
    expect(host.textContent).toContain("已按读回更新");
    expect((choice("inherit").querySelector("input") as HTMLInputElement).checked).toBe(true);
  });

  it("drops a late write for task A after switching to task B", async () => {
    let releaseA: ((response: Response) => void) | null = null;
    handle = (url, body) => {
      if (url.includes(TASK_MANAGER_PROFILE_SET_PATH) && body.task_id === "A") {
        return new Promise((resolve) => { releaseA = resolve; });
      }
      return ok({});
    };
    function Switcher() {
      const [taskId, setTaskId] = useState("A");
      const snapshot = taskId === "B"
        ? snap({ effective: { ...CUSTOM, source: "task" }, selectedProfileId: CUSTOM.profile_id })
        : snap();
      return (
        <>
          <button type="button" onClick={() => setTaskId("B")}>打开任务 B</button>
          <TaskBox taskId={taskId} snapshot={snapshot} onReread={async () => snap({
            effective: { ...OPUS, source: "task" },
            selectedProfileId: OPUS.profile_id,
          })} />
        </>
      );
    }
    await show(<Switcher />);
    await choose(OPUS.profile_id);
    await act(async () => { (button("切换模型") as HTMLButtonElement).click(); });
    await click("打开任务 B");
    expect(field("profile-id")).toBe(CUSTOM.profile_id);
    await act(async () => { releaseA?.(ok({ task_id: "A", profile_id: OPUS.profile_id })); });
    await settle();
    expect(field("profile-id")).toBe(CUSTOM.profile_id);
    expect(host.textContent).not.toContain("已按读回更新");
  });

  it("shows the gap when the set operation is missing and does not invent a read", async () => {
    handle = (url) => fail(404, "not_found", `no post ${url}`);
    await show(<TaskBox taskId="A" snapshot={snap()} onReread={async () => snap()} />);
    await choose(OPUS.profile_id);
    await click("切换模型");
    expect(host.querySelector('[aria-label="任务管家模型"]')).toBeNull();
    expect(button("切换模型")).toBeUndefined();
    expect(sets()).toHaveLength(1);
    expect(calls.every((call) => !call.url.includes("task_manager_profile_get"))).toBe(true);
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

  it("does not request a task manager profile while the panel is open", async () => {
    api.fetchTaskDetail.mockImplementation(async (id: string) => task(id));
    handle = () => ok({ activity: [], seq: 0 });
    await show(<WorkPage sub="manager" onNavigate={vi.fn()} />);
    expect(host.textContent).not.toContain("任务管家模型");
    expect(sets()).toHaveLength(0);

    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(calls.some((call) => call.url.includes("task_manager_profile"))).toBe(false);
    await click("任务详情");
    expect(host.querySelector('[aria-label="任务管家模型"]')).toBeNull();
    expect(calls.some((call) => call.url.includes("task_manager_profile"))).toBe(false);
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(calls.some((call) => call.url.includes("task_manager_profile"))).toBe(false);
    vi.useRealTimers();

    vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
      matches: String(query).includes("1280"),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })));
    calls.length = 0;
    await show(<WorkPage sub="task/A" onNavigate={vi.fn()} />);
    expect(host.querySelector('[aria-label="任务管家模型"]')).toBeNull();
    expect(calls.some((call) => call.url.includes("task_manager_profile"))).toBe(false);
    expect(sets()).toHaveLength(0);
  });
});
