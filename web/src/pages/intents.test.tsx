// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IntentsPage } from "@/pages/intents";
import { fetchProjectsList, fetchWorkflows, type Project, type Workflow } from "@/lib/api";
import {
  intentTypesHistory, intentTypesList, intentTypesRollback, intentTypesSave,
  intentsBuild, intentsList, intentsReject, intentsSettingsGet, intentsStats,
  type Intent, type IntentType, type IntentTypeVersion,
} from "@/lib/api-leads";
import { V1Error } from "@/lib/v1";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchProjectsList: vi.fn(), fetchWorkflows: vi.fn(),
}));
vi.mock("@/lib/api-leads", () => ({
  intentsSettingsGet: vi.fn(), intentsList: vi.fn(), intentsStats: vi.fn(), intentsUpdate: vi.fn(),
  intentsBuild: vi.fn(), intentsReject: vi.fn(), intentTypesList: vi.fn(), intentTypesHistory: vi.fn(),
  intentTypesCreate: vi.fn(), intentTypesSave: vi.fn(), intentTypesRollback: vi.fn(), intentTypesArchive: vi.fn(),
  intentTypesSetProjectTemplate: vi.fn(), intentBindingsList: vi.fn(), intentBindingsPut: vi.fn(),
  intentBindingsDelete: vi.fn(), intentAlertSendersList: vi.fn(), intentAlertSendersPut: vi.fn(),
  intentAlertSendersDelete: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const source = {
  chat: "设计群", chat_id: "oc_design", chat_kind: "group", sender: "Ada", sender_id: "ou_ada",
  sent_at: "2026-11-19T08:00:00Z", message_id: "om_1", link: "https://example.test/m/1",
};

function intent(extra: Partial<Intent> = {}): Intent {
  return {
    id: "intent-1", type: { key: "request", name: "请求" }, type_version: 1, title: "补一张看板",
    summary: "第一行\n第二行", confidence: "high", state: "pending", flagged: true,
    chat: { chat_id: "oc_design", name: "设计群", kind: "group" },
    project: { id: "project-a", name: "Example" }, workflow: { key: "fix", name: "Fix" },
    effective_workflow: { key: "fix", name: "Fix", source: "intent" }, candidates: [], merged_into: null, task: null,
    sources: [source], sources_total: 3, push_state: "none", expires_at: "2026-11-20T08:00:00Z",
    handled_at: null, handled_via: null, reject_reason: null, created_at: "2026-11-19T08:00:00Z", ...extra,
  };
}

const builtin: IntentType = {
  key: "request", builtin: true, enabled: true, archived: false, version: 1, name: "请求", guidance: "内置说明",
  priority: "normal", default_workflow: null, recommended: true, projects: [],
};
const custom: IntentType = {
  key: "custom_watch", builtin: false, enabled: true, archived: false, version: 2, name: "自定义", guidance: "自己的说明",
  priority: "urgent", default_workflow: "fix", recommended: false, projects: [],
};
const version: IntentTypeVersion = {
  version: 1, name: "自定义", guidance: "旧说明", priority: "normal", default_workflow: null,
  rolled_back_from: null, edited_via: "dashboard", created_at: "2026-11-18T09:00:00Z",
};

describe("Intents page", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.mocked(intentsSettingsGet).mockResolvedValue({ enabled: true, subscriptions: [] });
    vi.mocked(intentsList).mockResolvedValue({ intents: [intent()], more: false });
    vi.mocked(intentsStats).mockResolvedValue({ days: 7, types: [{
      key: "request", name: "请求", recognized: 4, tasked: 0, rejected: 0, expired: 0, merged: 0,
      adoption_rate: null, rejection_rate: null,
    }] });
    vi.mocked(intentTypesList).mockResolvedValue({ types: [builtin, custom] });
    vi.mocked(intentTypesHistory).mockResolvedValue({ key: "custom_watch", current: 2, versions: [version] });
    vi.mocked(fetchProjectsList).mockResolvedValue({
      projects: [{ project_id: "project-z", name: "Zed" } as Project, { project_id: "project-a", name: "Example" } as Project],
      projects_root: "",
    });
    vi.mocked(fetchWorkflows).mockResolvedValue([{ key: "fix", name: "Fix" } as Workflow]);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.clearAllMocks();
  });

  const show = async () => { await act(async () => { root.render(<IntentsPage />); }); };
  const button = (rootNode: ParentNode, text: string) =>
    [...rootNode.querySelectorAll("button")].find((item) => item.textContent === text);
  const setValue = (el: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, value: string) => {
    const prototype = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  };

  it("renders a pending intent and a dash for a null rate", async () => {
    await show();
    const text = container.textContent ?? "";
    expect(text).toContain("intent-1");
    expect(text).toContain("补一张看板");
    expect(text).toContain("待处理");
    expect(text).toContain("高");
    expect(text).toContain("需核对");
    expect(text).toContain("未推送");
    expect(text).toContain("另 2 条");
    expect(text).toContain("采纳率 —");
    expect(text).toContain("驳回率 —");
    expect(container.querySelector('a[href="https://example.test/m/1"]')?.getAttribute("target")).toBe("_blank");
  });

  it("sends the filters to the list", async () => {
    await show();
    expect(intentsList).toHaveBeenLastCalledWith({ state: ["pending"] });
    const labeled = (name: string) => [...container.querySelectorAll("label")].find((item) => item.textContent === name)!;
    await act(async () => labeled("已建任务").querySelector("input")!.click());
    await act(async () => setValue(container.querySelector('select[aria-label="类型"]')!, "request"));
    await act(async () => setValue(container.querySelector('select[aria-label="项目"]')!, "project-a"));
    await act(async () => setValue(container.querySelector('input[aria-label="会话"]')!, "设计群"));
    expect(intentsList).toHaveBeenLastCalledWith({
      state: ["pending", "tasked"], type: "request", project_id: "project-a", chat: "设计群",
    });
  });

  it("confirms a build and links the new task", async () => {
    vi.mocked(intentsBuild).mockResolvedValue({ intent: intent({ state: "tasked" }), task_id: "task-9" });
    await show();
    await act(async () => button(container, "建任务")!.click());
    const dialog = container.querySelector("[role=dialog]")!;
    expect(dialog.textContent).toContain("项目：Example");
    expect(dialog.textContent).toContain("工作流：Fix");
    expect(dialog.textContent).toContain("第一行");
    await act(async () => button(dialog, "建任务")!.click());
    expect(intentsBuild).toHaveBeenCalledWith({ intent_id: "intent-1", project_id: "project-a", workflow_key: "fix" });
    const link = container.querySelector('a[href="#tasks/task-9"]');
    expect(link?.textContent).toBe("任务 task-9");
  });

  it("rejects with a reason", async () => {
    vi.mocked(intentsReject).mockResolvedValue({ intent: intent({ state: "rejected", reject_reason: "不是现在" }) });
    await show();
    await act(async () => button(container, "驳回")!.click());
    const dialog = container.querySelector("[role=dialog]")!;
    await act(async () => setValue(dialog.querySelector("textarea")!, "不是现在"));
    await act(async () => button(dialog, "驳回")!.click());
    expect(intentsReject).toHaveBeenCalledWith({ intent_id: "intent-1", reason: "不是现在" });
  });

  it("lists an unbound intent's candidates before the other projects", async () => {
    vi.mocked(intentsList).mockResolvedValue({ intents: [intent({
      project: null, workflow: null, effective_workflow: null,
      candidates: [{ id: "cand", name: "Candidate first" }],
    })], more: false });
    await show();
    const select = container.querySelector<HTMLSelectElement>('select[aria-label="intent-1 项目"]')!;
    expect([...select.options].map((option) => option.value)).toEqual(["", "cand", "project-z", "project-a"]);
    expect(select.options[0].textContent).toBe("选择项目");
    expect(select.options[1].textContent).toBe("Candidate first");
    expect(button(container, "建任务")!.disabled).toBe(true);
  });

  it("saves a custom type, rolls back a version, and hides archive on a built-in type", async () => {
    vi.mocked(intentTypesSave).mockResolvedValue({ type: { ...custom, name: "自定义二" } });
    vi.mocked(intentTypesRollback).mockResolvedValue({ type: custom });
    await show();
    const tab = [...container.querySelectorAll('[role="tab"]')].find((item) => item.textContent === "类型")!;
    await act(async () => (tab as HTMLButtonElement).click());
    const open = (key: string) => [...container.querySelectorAll("button")].find((item) => item.textContent?.includes(key))!;
    await act(async () => open("request").click());
    expect(button(container, "删除")).toBeUndefined();
    await act(async () => open("custom_watch").click());
    expect(button(container, "删除")).toBeTruthy();
    expect(intentTypesHistory).toHaveBeenCalledWith({ key: "custom_watch" });
    expect(container.textContent).toContain("第 1 版");
    expect(container.textContent).toContain("界面");
    await act(async () => setValue(container.querySelector('input[aria-label="类型名称"]')!, "自定义二"));
    await act(async () => button(container, "保存")!.click());
    expect(intentTypesSave).toHaveBeenCalledWith({
      key: "custom_watch", name: "自定义二", guidance: "自己的说明", priority: "urgent", default_workflow: "fix",
    });
    await act(async () => button(container, "回退到这一版")!.click());
    const dialog = container.querySelector("[role=dialog]")!;
    await act(async () => button(dialog, "回退到这一版")!.click());
    expect(intentTypesRollback).toHaveBeenCalledWith({ key: "custom_watch", version: 1 });
  });

  it("says when nothing is pending", async () => {
    vi.mocked(intentsList).mockResolvedValue({ intents: [], more: false });
    await show();
    expect(container.textContent).toContain("待处理的意图没有了");
  });

  it("shows an operation error", async () => {
    vi.mocked(intentsList).mockRejectedValue(new Error("list down"));
    await show();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("list down");
  });

  it("shows that recognition is off, and a conflict from the list", async () => {
    vi.mocked(intentsSettingsGet).mockResolvedValue({ enabled: false, subscriptions: [{
      schedule_id: "sched-1", name: "意图识别", enabled: false, project_id: "project-a", target_ok: false,
    }] });
    vi.mocked(intentsList).mockRejectedValue(new V1Error(409, "conflict", "意图识别已关闭"));
    await show();
    expect(container.textContent).toContain("意图识别已关闭。建任务和驳回要等它打开");
    expect(container.querySelector('[data-state="conflict"]')?.textContent).toContain("意图识别已关闭");
  });
});
