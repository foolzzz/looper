// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryPage } from "@/pages/memory";
import * as memory from "@/lib/memory";
import type { MemoryCandidate, MemoryDetail, MemorySettingsView } from "@/lib/memory";
import { V1Error } from "@/lib/v1";

vi.mock("@/lib/memory", () => ({
  memoryList: vi.fn(), memoryGet: vi.fn(), memoryConfirm: vi.fn(), memoryConfirmMany: vi.fn(), memoryReject: vi.fn(),
  memoryEdit: vi.fn(), memoryRetry: vi.fn(), memoryRevive: vi.fn(), memoryDelete: vi.fn(), memorySettingsGet: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const HASH = "a1b2c3d4".repeat(8);
const ON: MemorySettingsView = { configured: true, enabled: true, settings: null, problem: null };

function candidate(extra: Partial<MemoryCandidate> = {}): MemoryCandidate {
  return {
    id: "M-0001", kind: "preference", state: "pending", reason: null, destination: "vault", source_kind: "manager_turn",
    source_class: "operator", statement: "Weekly reports: conclusion first", why: "easier to skim", keywords: [],
    title: "Weekly reports", note_path: "mem/prefs/weekly.md", revision: 1, content_hash: HASH, untrusted: false,
    flagged: false, hard_rule: false, project_id: null, manager_id: null, commit: null, merged_into: null,
    supersedes: null, expires_at: null, valid_until: null, written_at: null, purged: false, sources_total: 1,
    created_at: "2026-11-20T08:00:00Z", updated_at: "2026-11-20T08:00:00Z", ...extra,
  };
}

function detail(extra: Partial<MemoryDetail> = {}): MemoryDetail {
  return {
    ...candidate(), sources: [
      { kind: "task", source_class: "operator", quote: "the build needs make gen", channel: null, chat: null, message_id: null, said_at: null, task_id: "task-9", lead: null },
    ],
    events: [{ action: "propose", actor: "manager", via: "lark", revision: null, content_hash: null, note: null, at: "2026-11-20T08:00:00Z" }],
    current: {
      revision: 1, made_by: "draft", title: "Weekly reports", content_hash: HASH, large_removal: false, created_at: "2026-11-20T08:00:00Z",
      changes: [
        { op: "create_note", path: "mem/prefs/weekly.md", content: "Conclusion first.\n" },
        { op: "append_line", path: "mem/prefs/_index.md", under: "## Index", line: "- [[weekly]]" },
      ],
      diff_stats: { "mem/prefs/weekly.md": { added: 1, removed: 0 } },
      diffs: { "mem/prefs/weekly.md": "--- /dev/null\n+++ b/mem/prefs/weekly.md\n@@ -0,0 +1 @@\n+Conclusion first.\n" },
      diffs_problem: null,
    },
    ...extra,
  };
}

let container: HTMLDivElement;
let root: Root;
let items: MemoryCandidate[];
let details: Record<string, MemoryDetail>;
const navigate = vi.fn();

async function render(sub = "") {
  await act(async () => { root.render(<MemoryPage sub={sub} onNavigate={navigate} />); });
}
const text = () => container.textContent ?? "";
const button = (name: string) => [...container.querySelectorAll("button")].find((b) => b.textContent?.trim() === name) as HTMLButtonElement | undefined;
async function click(el: Element | undefined) {
  expect(el).toBeTruthy();
  await act(async () => { (el as HTMLElement).click(); });
}

beforeEach(() => {
  items = [candidate()];
  details = { "M-0001": detail() };
  vi.mocked(memory.memorySettingsGet).mockResolvedValue(ON);
  vi.mocked(memory.memoryList).mockImplementation(async () => ({ candidates: items, more: false }));
  vi.mocked(memory.memoryGet).mockImplementation(async (id: string) => details[id]);
  navigate.mockClear();
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

describe("Memory page list", () => {
  it("groups by state with pending first, blocked and conflict apart, and says why in words", async () => {
    items = [
      candidate({ id: "M-0005", state: "written" }),
      candidate({ id: "M-0004", state: "blocked", reason: "dirty_target" }),
      candidate({ id: "M-0003", state: "conflict", reason: "anchor_missing" }),
      candidate({ id: "M-0002", state: "expired" }),
      candidate({ id: "M-0001", state: "pending", untrusted: true, flagged: true, hard_rule: true }),
    ];
    await render();
    const headings = [...container.querySelectorAll("h2")].map((h) => h.textContent);
    expect(headings.map((h) => h?.replace(/\d+$/, ""))).toEqual(["待确认", "受阻与冲突", "已过期", "已写入"]);
    expect(text()).toContain("目标文件有没提交的改动");
    expect(text()).toContain("要追加或替换的那一行在笔记里找不到了");
    for (const word of ["来自消息", "有指令", "硬规则"]) expect(text()).toContain(word);
    expect(text()).toContain("mem/prefs/weekly.md");
    expect(text()).toContain("来自对话");
    // Only pending rows can be ticked.
    expect(container.querySelectorAll('input[type="checkbox"]')).toHaveLength(1);
  });

  it("shows no counts it was not given", async () => {
    await render();
    expect(text()).not.toMatch(/近 7 天|7 天内/);
  });

  it("says loading, then empty in words", async () => {
    items = [];
    await render();
    expect(text()).toContain("现在没有待处理的记忆");
  });

  it("shows a failed read as an alert, not an empty list, and retries", async () => {
    vi.mocked(memory.memoryList).mockRejectedValueOnce(new Error("database is down"));
    await render();
    const alert = container.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain("database is down");
    expect(text()).not.toContain("现在没有待处理的记忆");
    await click(button("重试"));
    expect(text()).toContain("M-0001");
  });

  it("says not configured and turns every action off", async () => {
    vi.mocked(memory.memorySettingsGet).mockResolvedValue({ configured: false, enabled: false, settings: null, problem: null });
    await render("M-0001");
    expect(container.querySelector('[data-state="not-configured"]')?.textContent).toContain("还没有配置");
    expect(container.querySelector('a[href="#settings/vault"]')).toBeTruthy();
    expect(button("确认写入")!.disabled).toBe(true);
    expect(button("驳回")!.disabled).toBe(true);
    expect((container.querySelector('input[type="checkbox"]') as HTMLInputElement).disabled).toBe(true);
    expect(button("确认所选")!.disabled).toBe(true);
  });

  it("shows the setting's problem in words", async () => {
    vi.mocked(memory.memorySettingsGet).mockResolvedValue({ ...ON, problem: "the vault is gone" });
    await render();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("the vault is gone");
  });
});

describe("Memory page detail", () => {
  it("shows the statement, the server's per-file diff, sources, events and the first 8 hash characters", async () => {
    await render("M-0001");
    expect(text()).toContain("Weekly reports: conclusion first");
    expect(text()).toContain("理由：easier to skim");
    expect(container.querySelector('pre[aria-label="差异"]')?.textContent).toContain("+Conclusion first.");
    // A change without a server diff shows the change as written.
    expect(text()).toContain("- [[weekly]]");
    expect(text()).toContain("指纹 a1b2c3d4");
    expect(text()).not.toContain(HASH);
    expect(container.querySelector('a[href="#tasks/task-9"]')).toBeTruthy();
    expect(text()).toContain("the build needs make gen");
    expect(text()).toContain("提议");
    expect(text()).not.toContain("重新起草");
  });

  it("says why there is no diff and shows large removals and flags", async () => {
    details["M-0001"] = detail({ flagged: true, current: { ...detail().current!, diffs: {}, diffs_problem: "the vault no longer matches", large_removal: true } });
    await render("M-0001");
    expect(text()).toContain("看不到和知识库现状的差异：the vault no longer matches");
    expect(text()).toContain("大段删除");
  });

  it("confirm sends the rendered hash", async () => {
    vi.mocked(memory.memoryConfirm).mockResolvedValue(detail({ state: "confirmed" }));
    await render("M-0001");
    await click(button("确认写入"));
    expect(memory.memoryConfirm).toHaveBeenCalledWith("M-0001", HASH);
    expect(text()).toContain("已确认，等待写入");
  });

  it("a 409 says the content changed and reads the new version", async () => {
    vi.mocked(memory.memoryConfirm).mockRejectedValue(new V1Error(409, "conflict", "this candidate changed since it was shown"));
    await render("M-0001");
    const reads = vi.mocked(memory.memoryGet).mock.calls.length;
    await click(button("确认写入"));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("打开之后内容改过，请看新版本");
    expect(vi.mocked(memory.memoryGet).mock.calls.length).toBe(reads + 1);
  });

  it("a disabled refusal explains and links to the setting", async () => {
    vi.mocked(memory.memoryConfirm).mockRejectedValue(new V1Error(403, "forbidden", "memory writing is off"));
    await render("M-0001");
    await click(button("确认写入"));
    const alert = container.querySelector('[role="alert"]')!;
    expect(alert.textContent).toContain("记忆写入没有打开");
    expect(alert.querySelector('a[href="#settings/vault"]')).toBeTruthy();
  });

  it("reject asks for a reason", async () => {
    vi.mocked(memory.memoryReject).mockResolvedValue(detail({ state: "rejected" }));
    await render("M-0001");
    await click(button("驳回"));
    const input = container.querySelector("#memory-reject-reason") as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "not useful");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const confirm = [...container.querySelectorAll('[role="dialog"] button')].find((b) => b.textContent === "驳回");
    await click(confirm);
    expect(memory.memoryReject).toHaveBeenCalledWith("M-0001", "not useful");
  });

  it("edit saves per-change text, with or without writing", async () => {
    vi.mocked(memory.memoryEdit).mockResolvedValue(detail({ revision: 2 }));
    await render("M-0001");
    await click(button("编辑"));
    const area = container.querySelector("#memory-edit-0") as HTMLTextAreaElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(area, "Conclusion first, then why.\n");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await click(button("保存"));
    const sent = vi.mocked(memory.memoryEdit).mock.calls[0];
    expect(sent[0]).toBe("M-0001");
    expect(sent[1]).toBe(HASH);
    expect(sent[2][0]).toMatchObject({ op: "create_note", path: "mem/prefs/weekly.md", content: "Conclusion first, then why.\n" });
    expect(sent[2][1]).toMatchObject({ op: "append_line", under: "## Index" });
    expect(sent[3]).toBe(false);
    await click(button("编辑"));
    await click(button("保存并写入"));
    expect(vi.mocked(memory.memoryEdit).mock.calls[1][3]).toBe(true);
  });

  it("offers retry, revive and delete only where the state allows", async () => {
    const check = async (state: string, shown: string[], hidden: string[]) => {
      details["M-0001"] = detail({ state, reason: state === "blocked" ? "git_failed" : null });
      items = [candidate({ state })];
      act(() => root.unmount());
      root = createRoot(container);
      await render("M-0001");
      for (const name of shown) expect(button(name), `${state} shows ${name}`).toBeTruthy();
      for (const name of hidden) expect(button(name), `${state} hides ${name}`).toBeFalsy();
    };
    await check("pending", ["确认写入", "编辑", "驳回", "删除"], ["重试", "恢复"]);
    await check("blocked", ["重试", "驳回", "删除"], ["确认写入", "编辑", "恢复"]);
    await check("expired", ["恢复", "驳回", "删除"], ["确认写入", "重试", "编辑"]);
    await check("conflict", ["驳回", "删除"], ["确认写入", "重试", "恢复", "编辑"]);
    await check("writing", [], ["确认写入", "重试", "恢复", "驳回", "删除"]);
    await check("written", ["删除"], ["确认写入", "重试", "恢复", "驳回"]);
  });

  it("retry and revive call their operations", async () => {
    vi.mocked(memory.memoryRetry).mockResolvedValue(detail({ state: "confirmed" }));
    vi.mocked(memory.memoryRevive).mockResolvedValue(detail());
    details["M-0001"] = detail({ state: "blocked", reason: "git_failed" });
    await render("M-0001");
    await click(button("重试"));
    expect(memory.memoryRetry).toHaveBeenCalledWith("M-0001");
    details["M-0001"] = detail({ state: "expired" });
    act(() => root.unmount());
    root = createRoot(container);
    await render("M-0001");
    await click(button("恢复"));
    expect(memory.memoryRevive).toHaveBeenCalledWith("M-0001");
  });

  it("delete says a tombstone stays and written notes are untouched, then goes back to the list", async () => {
    vi.mocked(memory.memoryDelete).mockResolvedValue({ candidate: "M-0001", deleted: true });
    await render("M-0001");
    await click(button("删除"));
    const dialog = container.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain("防止它再被提议的指纹");
    expect(dialog.textContent).toContain("已经写进知识库的笔记不会动");
    await click([...dialog.querySelectorAll("button")].find((b) => b.textContent === "删除"));
    expect(memory.memoryDelete).toHaveBeenCalledWith("M-0001");
    expect(navigate).toHaveBeenCalledWith("");
  });

  it("a failed detail read is an alert", async () => {
    vi.mocked(memory.memoryGet).mockRejectedValue(new V1Error(404, "not_found", "no memory candidate M-0001"));
    await render("M-0001");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("no memory candidate M-0001");
  });
});

describe("Memory page follows the writer", () => {
  it("re-reads while a candidate is confirmed or writing, and shows where it ended", async () => {
    vi.useFakeTimers();
    try {
      items = [candidate({ state: "confirmed" })];
      details["M-0001"] = detail({ state: "confirmed" });
      await render("M-0001");
      expect(text()).toContain("已确认，等待写入");
      items = [candidate({ state: "blocked", reason: "dirty_target" })];
      details["M-0001"] = detail({ state: "blocked", reason: "dirty_target" });
      await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
      expect(text()).toContain("写入受阻");
      expect(button("重试")).toBeTruthy();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Memory page batch", () => {
  it("confirms the ticked rows with their hashes and shows each result next to its row", async () => {
    items = [candidate({ id: "M-0002", content_hash: "b".repeat(64) }), candidate({ id: "M-0001" })];
    vi.mocked(memory.memoryConfirmMany).mockResolvedValue([
      { candidate: "M-0002", ok: false, result: null, error: { code: "conflict", message: "changed since it was shown" } },
      { candidate: "M-0001", ok: true, result: { candidate: detail({ state: "confirmed" }) }, error: null },
    ]);
    await render();
    expect(button("确认所选")!.disabled).toBe(true);
    for (const box of container.querySelectorAll('input[type="checkbox"]')) await click(box);
    expect(button("确认所选（2）")).toBeTruthy();
    await click(button("确认所选（2）"));
    expect(memory.memoryConfirmMany).toHaveBeenCalledWith([
      { candidate: "M-0002", content_hash: "b".repeat(64) }, { candidate: "M-0001", content_hash: HASH },
    ]);
    const results = [...container.querySelectorAll("[data-result]")].map((el) => [el.getAttribute("data-result"), el.textContent]);
    expect(results[0][0]).toBe("failed");
    expect(results[0][1]).toContain("打开之后内容改过");
    expect(results[1]).toEqual(["ok", "✓ 已确认，等待写入"]);
  });

  it("has no select-all and confirms nothing with nothing ticked", async () => {
    await render();
    expect(text()).not.toMatch(/全选|全部确认/);
    expect(button("确认所选")!.disabled).toBe(true);
  });
});

describe("Memory page narrow screens", () => {
  it("shows the list or the detail alone below lg, with a way back", async () => {
    await render("M-0001");
    const list = container.querySelector("#memory-filter")!.closest("div.min-w-0.space-y-4")!;
    expect(list.className).toContain("max-lg:hidden");
    await click(button("返回列表"));
    expect(navigate).toHaveBeenCalledWith("");
  });
});
