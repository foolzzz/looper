// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemorySettingsSection } from "@/components/memory-settings";
import * as memory from "@/lib/memory";
import type { MemorySettingsView } from "@/lib/memory";
import { fetchChannelConfigs, type ChannelConfig } from "@/lib/api";
import { V1Error } from "@/lib/v1";

vi.mock("@/lib/memory", async (original) => ({
  ...await original<typeof import("@/lib/memory")>(),
  memorySettingsGet: vi.fn(), memorySettingsPut: vi.fn(), memoryCheck: vi.fn(), memoryConfirmSave: vi.fn(),
}));
vi.mock("@/lib/api", async (original) => ({ ...await original<typeof import("@/lib/api")>(), fetchChannelConfigs: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const OFF: MemorySettingsView = { configured: false, enabled: false, settings: null, problem: null };
const ON: MemorySettingsView = {
  configured: true, enabled: true, problem: null,
  settings: {
    enabled: true, root: "mem", rules_files: ["mem/AGENTS.md"], required_frontmatter: ["kind"],
    targets: { preference: { shape: "note", folders: ["prefs"], index: "prefs/_index.md" }, fact: { shape: "entry", files: ["facts.md"] } },
    sources: { manager: true, tasks: false, leads: false }, branch: "main", worktree_root: "/srv/wt", commit_prefix: "mem",
    push_channel: "lark-1", pending_days: 7, draft_budget_usd: 0.15, daily_budget_usd: 1,
  },
};
const bot = (id: string, type: string, operator?: string): ChannelConfig => ({
  channel_id: id, channel_type: type, display_name: `Bot ${id}`, enabled: true, config: operator ? { operator_sender_id: operator } : {},
  manager_id: "m", manager_name: null, builtin: false, created_at: "", updated_at: "",
});

let container: HTMLDivElement;
let root: Root;
const text = () => container.textContent ?? "";
const button = (name: string) => [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;
async function click(el: Element) { await act(async () => { (el as HTMLElement).click(); }); }
async function type(el: HTMLInputElement | HTMLSelectElement, value: string) {
  await act(async () => {
    const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}
const field = (label: string) => container.querySelector(`[aria-label="${label}"]`) as HTMLInputElement;
async function render() { await act(async () => { root.render(<MemorySettingsSection />); }); }

beforeEach(() => {
  vi.mocked(memory.memorySettingsGet).mockResolvedValue(ON);
  vi.mocked(fetchChannelConfigs).mockResolvedValue([bot("lark-1", "lark", "ou_op"), bot("lark-2", "lark"), bot("web-1", "web", "x")]);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); vi.clearAllMocks(); });

describe("Memory settings section", () => {
  it("shows the saved fields, the read-only branch and worktree folder, and only bots with an operator id", async () => {
    await render();
    expect((container.querySelector('input[placeholder="codex-mem"]') as HTMLInputElement).value).toBe("mem");
    expect((container.querySelector('input[placeholder="AGENTS.md"]') as HTMLInputElement).value).toBe("mem/AGENTS.md");
    expect((field("偏好的写法") as unknown as HTMLSelectElement).value).toBe("note");
    expect((field("偏好的文件夹")).value).toBe("prefs");
    expect((field("事实的页面")).value).toBe("facts.md");
    expect((field("经验的写法") as unknown as HTMLSelectElement).value).toBe("");
    expect(container.querySelector('[aria-label="和 Manager 的对话"]')!.getAttribute("aria-checked")).toBe("true");
    expect(container.querySelector('[aria-label="消息线索"]')!.getAttribute("aria-checked")).toBe("false");
    const options = [...container.querySelectorAll("select option")].map((o) => o.textContent);
    expect(options).toContain("Bot lark-1");
    expect(options.some((o) => o?.includes("lark-2") || o?.includes("web-1"))).toBe(false);
    expect(text()).toContain("main");
    expect(text()).toContain("/srv/wt");
    expect(container.querySelectorAll("dd input")).toHaveLength(0);
  });

  it("says not configured, and a problem in an alert", async () => {
    vi.mocked(memory.memorySettingsGet).mockResolvedValue(OFF);
    await render();
    expect(text()).toContain("没有配置");
    act(() => root.unmount());
    root = createRoot(container);
    vi.mocked(memory.memorySettingsGet).mockResolvedValue({ ...ON, problem: "the vault is gone" });
    await render();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("the vault is gone");
  });

  it("saves the whole setting without the branch, keeping the stored worktree folder", async () => {
    vi.mocked(memory.memorySettingsPut).mockResolvedValue(ON);
    await render();
    await type(container.querySelector('input[placeholder="codex-mem"]') as HTMLInputElement, "mem2");
    await click(button("保存"));
    const body = vi.mocked(memory.memorySettingsPut).mock.calls[0][0];
    expect(body).toMatchObject({
      enabled: true, root: "mem2", rules_files: ["mem/AGENTS.md"], required_frontmatter: ["kind"], push_channel: "lark-1",
      sources: { manager: true, tasks: false, leads: false }, pending_days: 7, draft_budget_usd: 0.15, daily_budget_usd: 1,
      worktree_root: "/srv/wt", targets: { preference: { shape: "note", folders: ["prefs"], index: "prefs/_index.md" }, fact: { shape: "entry", files: ["facts.md"] } },
    });
    expect(body).not.toHaveProperty("branch");
    expect(text()).toContain("已保存");
  });

  it("shows the impact of a call that asks first, and runs it only after confirmation", async () => {
    const pending = { status: "needs_confirmation" as const, confirmation_id: "c1", expires_in_seconds: 60, impact: "写入会提交到 main 分支，每天最多 $1。", session: "s1" };
    vi.mocked(memory.memorySettingsPut).mockRejectedValue(new memory.MemoryNeedsConfirmation(pending));
    vi.mocked(memory.memoryConfirmSave).mockResolvedValue(ON);
    await render();
    await click(button("保存"));
    const dialog = container.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain("写入会提交到 main 分支");
    expect(memory.memoryConfirmSave).not.toHaveBeenCalled();
    await click([...dialog.querySelectorAll("button")].find((b) => b.textContent === "确认并保存")!);
    expect(memory.memoryConfirmSave).toHaveBeenCalledWith(pending);
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(text()).toContain("已保存");
  });

  it("shows a refused save in an alert", async () => {
    vi.mocked(memory.memorySettingsPut).mockRejectedValue(new V1Error(400, "invalid", "root is required"));
    await render();
    await click(button("保存"));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("root is required");
  });

  it("refuses a non-numeric budget before calling the server", async () => {
    await render();
    await type(container.querySelector("input[inputmode='decimal']") as HTMLInputElement, "lots");
    await click(button("保存"));
    expect(memory.memorySettingsPut).not.toHaveBeenCalled();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("要填数字");
  });

  it("the check button lists each check as passed or not with its detail", async () => {
    vi.mocked(memory.memoryCheck).mockResolvedValue({ configured: true, enabled: true, ok: false, checks: [
      { name: "git", ok: true, detail: "/srv/vault" }, { name: "branch", ok: false, detail: "the main checkout is on dev, not main" },
    ] });
    await render();
    await click(button("检查"));
    const items = [...container.querySelectorAll('ul[aria-label="检查结果"] li')].map((li) => li.textContent);
    expect(items[0]).toContain("git 仓库：通过");
    expect(items[1]).toContain("分支：没通过");
    expect(items[1]).toContain("the main checkout is on dev");
  });

  it("a failed check or read is an alert", async () => {
    vi.mocked(memory.memoryCheck).mockRejectedValue(new Error("no database"));
    await render();
    await click(button("检查"));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("no database");
    act(() => root.unmount());
    root = createRoot(container);
    vi.mocked(memory.memorySettingsGet).mockRejectedValue(new Error("unreachable"));
    await render();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("unreachable");
  });
});
