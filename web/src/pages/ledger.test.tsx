// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LedgerPage } from "@/pages/ledger";
import { ledgerPage, ledgerGet, ledgerAdd, ledgerAction, type LedgerItem, type LedgerPageResult } from "@/lib/api-ledger";
import { useLedger } from "@/lib/use-ledger";
import { V1Error } from "@/lib/v1";
vi.mock("@/lib/api-ledger", () => ({ ledgerPage: vi.fn(), ledgerGet: vi.fn(), ledgerAdd: vi.fn(), ledgerAction: vi.fn() }));
vi.mock("@/lib/use-ledger", () => ({ useLedger: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean; }).IS_REACT_ACT_ENVIRONMENT = true;
const changed = vi.fn(), refresh = vi.fn();
const state = { enabled: true, count: 3, stale: false, error: null, revision: 0, changed, refresh };
const item = (extra: Partial<LedgerItem> = {}): LedgerItem => ({ id: "L-0001", number: 1, kind: "pending", status: "open", title: "合成事项", detail: "详情", assurance: "user", source: null, owner: "user", project_id: null, due_at: null, overdue: false, refs: [], superseded_by: null, added_via: "api", created_at: "2026-11-23T10:00:00Z", updated_at: "2026-11-23T10:00:00Z", closed_at: null, ...extra });
const page = (items: LedgerItem[] = [], more = 0, next_cursor: string | null = null): LedgerPageResult => ({ items, more, next_cursor, counts: { open: 3, done: 0, cancelled: 0, expired: 0, superseded: 0 }, attention_count: 1, missing_operator_ids: [] });
let root: Root, host: HTMLDivElement, query: string;
const onQuery = vi.fn((value: string) => {
  query = value;
  root.render(<LedgerPage query={query} onQuery={onQuery} />);
});
async function render(value = query) {
  query = value;
  await act(async () => {
    root.render(<LedgerPage query={value} onQuery={onQuery} />);
  });
}
async function press(text: string, scope: ParentNode = host) {
  await act(async () => {
    const button = [...scope.querySelectorAll("button")].find((b) => b.textContent?.trim() === text || b.getAttribute("aria-label") === text);
    expect(button, text).toBeTruthy();
    button!.click();
  });
}
async function input(label: string, value: string) {
  await act(async () => {
    const el = host.querySelector(`[aria-label="${label}"]`) as HTMLInputElement;
    expect(el, label).toBeTruthy();
    const setter = Object.getOwnPropertyDescriptor(el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, "value")!.set!;
    setter.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}
beforeEach(() => {
  query = "";
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  vi.mocked(useLedger).mockReturnValue(state);
  vi.mocked(ledgerPage).mockResolvedValue(page());
  vi.mocked(ledgerGet).mockResolvedValue({ item: item(), events: [] });
  vi.mocked(ledgerAdd).mockResolvedValue(item());
  vi.mocked(ledgerAction).mockResolvedValue(item());
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.resetAllMocks();
});
describe("Ledger page", () => {
  it("shows the three groups and follows more without losing filters", async () => {
    vi.mocked(ledgerPage).mockImplementation(async (args = {}) => args.section === "attention" ? args.cursor ? page([item({ id: "L-0011", title: "后一页" })]) : page(Array.from({ length: 10 }, (_, n) => item({ id: `L-${n + 1}`, number: n + 1, title: `事项${n}` })), 1, "next") : page());
    await render();
    expect([...host.querySelectorAll("h2")].map((h) => h.textContent)).toEqual(["要你拍板", "进行中", "最近决定"]);
    expect(host.querySelectorAll('[data-ledger-row]').length).toBe(10);
    await press("另有 1 项");
    expect(host.querySelectorAll('[data-ledger-row]').length).toBe(11);
    expect(host.textContent).not.toContain("另有 0 项");
    await input("类型筛选", "decision");
    expect(query).toContain("kind=decision");
    expect(ledgerPage).toHaveBeenLastCalledWith(expect.objectContaining({ section: "all", kind: "decision", status: "open" }), expect.anything());
    await render("");
    expect(host.querySelectorAll("h2").length).toBe(3);
  });
  it("renders off loading empty filtered-empty error and forbidden honestly", async () => {
    vi.mocked(useLedger).mockReturnValue({ ...state, enabled: false });
    await render();
    expect(host.textContent).toContain("Settings → Chat");
    expect(ledgerPage).not.toHaveBeenCalled();
    vi.mocked(useLedger).mockReturnValue({ ...state, enabled: null });
    await render();
    expect(host.textContent).toContain("读取中");
    vi.mocked(useLedger).mockReturnValue(state);
    await render();
    expect(host.textContent).toContain("没有未结的事项");
    await render("kind=decision");
    expect(host.textContent).toContain("没有符合筛选的事项");
    vi.mocked(ledgerPage).mockRejectedValue(new Error("断开"));
    await render("status=done");
    expect(host.textContent).toContain("断开");
    expect(host.textContent).not.toContain("没有符合筛选的事项");
    vi.mocked(ledgerPage).mockRejectedValue(new V1Error(403, "forbidden", "拒绝"));
    await render("status=all");
    expect(host.textContent).toContain("无权访问");
    expect(host.textContent).not.toContain("新增事项");
  });
  it("offers only legal own and sourced actions", async () => {
    vi.mocked(ledgerGet).mockResolvedValue({ item: item({ kind: "decision", assurance: "relayed" }), events: [] });
    await render("item=L-0001");
    expect(host.textContent).toContain("确认");
    expect([...host.querySelectorAll("button")].some((b) => b.textContent === "完成")).toBe(false);
    vi.mocked(ledgerGet).mockResolvedValue({ item: item({ source: { kind: "gate", ref: "g1", state: "open", href: "#tasks/t1?gate=s1" } }), events: [] });
    await render("item=L-0002");
    const drawer = host.querySelector('[role="dialog"]')!;
    expect(drawer.querySelector('a[href="#tasks/t1?gate=s1"]')?.textContent).toBe("去处理");
    for (const action of ["完成", "撤销", "确认"]) expect([...drawer.querySelectorAll("button")].map((b) => b.textContent)).not.toContain(action);
    await press("编辑到期、负责人");
    expect(drawer.querySelector('[aria-label="标题"]')).toBeNull();
  });
  it("writes then reads back and keeps failed edits", async () => {
    await render("item=L-0001");
    await press("编辑");
    await input("标题", "修订标题");
    vi.mocked(ledgerAction).mockRejectedValueOnce(new V1Error(409, "conflict", "已经修改"));
    await press("保存");
    expect((host.querySelector('[aria-label="标题"]') as HTMLInputElement).value).toBe("修订标题");
    expect(host.textContent).toContain("已经修改");
    expect(ledgerGet).toHaveBeenCalledTimes(2);
    vi.mocked(ledgerAction).mockRejectedValueOnce(new Error("响应丢失"));
    await press("保存");
    expect(host.textContent).toContain("未确认成功");
    expect((host.querySelector('[aria-label="标题"]') as HTMLInputElement).value).toBe("修订标题");
    let finish!: (value: LedgerItem) => void;
    vi.mocked(ledgerAction).mockImplementationOnce(() => new Promise((resolve) => {
      finish = resolve;
    }));
    await press("保存");
    expect((host.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(true);
    await act(async () => {
      finish(item({ title: "修订标题" }));
    });
    expect(changed).toHaveBeenCalled();
    expect(ledgerGet).toHaveBeenCalledTimes(3);
    expect(host.querySelector('[aria-label="标题"]')).toBeNull();
  });
  it("opens a deep link and restores focus on close", async () => {
    vi.mocked(ledgerPage).mockImplementation(async (args = {}) => page(args.section === "all" ? [item()] : []));
    await render("kind=pending");
    const row = host.querySelector('[data-ledger-row]') as HTMLButtonElement;
    row.focus();
    await act(async () => {
      row.click();
    });
    expect(query).toContain("kind=pending");
    expect(query).toContain("item=L-0001");
    expect(ledgerGet).toHaveBeenCalledWith("L-0001", expect.anything());
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    });
    expect(query).toBe("kind=pending");
    expect(document.activeElement).toBe(row);
    vi.mocked(ledgerGet).mockRejectedValue(new V1Error(404, "not_found", "missing"));
    await render("kind=pending&item=L-9999");
    expect(host.textContent).toContain("未找到");
  });
  it("shows missing operator identity and CLI provenance", async () => {
    const result = page([item({ added_via: "cli", assurance: "relayed" })]);
    result.missing_operator_ids = [{ channel_id: "bot", channel_type: "telegram", name: "合成机器人" }];
    vi.mocked(ledgerPage).mockResolvedValue(result);
    vi.mocked(ledgerGet).mockResolvedValue({ item: item({ added_via: "cli", refs: [{ kind: "url", ref: "javascript:alert(1)", label: "不安全链接" }, { kind: "url", ref: "https://example.test", label: "文档" }] }), events: [{ id: 1, action: "add", actor_kind: "user", actor_id: "cli", relayed_by: null, via: "cli", session_label: "合成会话", quote: "原话", note: null, changes: null, created_at: "2026-11-23T10:00:00Z" }] });
    await render("item=L-0001");
    expect(host.textContent).toContain("合成机器人");
    expect(host.textContent).toContain("命令行 · 合成会话");
    expect(host.textContent).toContain("转述");
    expect(host.querySelector('a[href="#settings/channels"]')).toBeTruthy();
    expect(host.querySelector('a[href^="javascript:"]')).toBeNull();
    expect(host.querySelector('a[href="https://example.test"]')?.getAttribute("rel")).toBe("noopener noreferrer");
  });
  it("normalizes unknown query values without issuing an invalid request", async () => {
    await render("kind=wrong&status=bad&unknown=secret");
    expect(query).toBe("");
    expect(vi.mocked(ledgerPage).mock.calls.every(([args = {}]) => args.kind !== ("wrong" as never) && args.status !== ("bad" as never))).toBe(true);
  });
  it("keeps read pages and retries the same cursor after a failed next page", async () => {
    vi.mocked(ledgerPage).mockImplementation(async (args = {}) => {
      if (!args.cursor) return page([item()], 1, "stable-cursor");
      throw new Error("翻页离线");
    });
    await render("kind=pending");
    await press("另有 1 项");
    expect(host.querySelectorAll('[data-ledger-row]').length).toBe(1);
    expect(host.textContent).toContain("翻页离线");
    vi.mocked(ledgerPage).mockResolvedValue(page([item({ id: "L-0002", title: "第二项" })]));
    await press("重试");
    expect(ledgerPage).toHaveBeenLastCalledWith(expect.objectContaining({ section: "all", kind: "pending", cursor: "stable-cursor" }), expect.anything());
    expect(host.querySelectorAll('[data-ledger-row]').length).toBe(2);
  });
  it("retries a failed refresh from the first page while keeping loaded entries", async () => {
    vi.mocked(ledgerPage).mockResolvedValue(page([item()], 1, "next-page"));
    await render("kind=pending");
    vi.mocked(ledgerPage).mockRejectedValueOnce(new Error("刷新离线"));
    vi.mocked(useLedger).mockReturnValue({ ...state, revision: 1 });
    await render();
    expect(host.textContent).toContain("刷新离线");
    expect(host.querySelectorAll('[data-ledger-row]').length).toBe(1);
    vi.mocked(ledgerPage).mockResolvedValue(page([item({ title: "刷新后的事项" })], 1, "new-next-page"));
    await press("重试");
    const [request] = vi.mocked(ledgerPage).mock.calls.at(-1)!;
    expect(request).not.toHaveProperty("cursor");
    expect(host.querySelectorAll('[data-ledger-row]').length).toBe(1);
    expect(host.textContent).toContain("刷新后的事项");
  });
  it("ignores an old filter response and commits typed owner filters only when complete", async () => {
    let finish!: (value: LedgerPageResult) => void;
    vi.mocked(ledgerPage).mockImplementationOnce(() => new Promise((resolve) => {
      finish = resolve;
    }));
    await render("kind=pending");
    vi.mocked(ledgerPage).mockResolvedValue(page([item({ title: "当前筛选" })]));
    await render("kind=decision");
    await act(async () => {
      finish(page([item({ title: "旧筛选" })]));
    });
    expect(host.textContent).toContain("当前筛选");
    expect(host.textContent).not.toContain("旧筛选");
    await input("负责人筛选", "task:");
    expect((host.querySelector('[aria-label="负责人筛选"]') as HTMLInputElement).value).toBe("task:");
    await input("负责人筛选", "task:demo");
    await act(async () => {
      host.querySelector('[aria-label="负责人筛选"]')!.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    });
    expect(query).toContain("owner=task%3Ademo");
  });
  it.each(["pending", "decision", "work_item"] as const)("adds a %s and reads it back before showing its detail", async (kind) => {
    vi.mocked(ledgerAdd).mockResolvedValue(item({ kind }));
    await render();
    await press("新增事项");
    await input("事项类型", kind);
    await input("标题", "新增合成事项");
    if (kind === "decision") expect(host.textContent).toContain("保存为本人已确认的决定");
    await press("保存");
    expect(ledgerAdd).toHaveBeenCalledWith(expect.objectContaining({ title: "新增合成事项", kind }));
    expect(ledgerGet).toHaveBeenCalledWith("L-0001", expect.anything());
    expect(changed).toHaveBeenCalled();
    expect(query).toContain("item=L-0001");
    expect(host.querySelector('[aria-label="标题"]')).toBeNull();
  });
  it.each([400, 403])("preserves invalid edits and stops a forbidden submission (%s)", async (status) => {
    await render("item=L-0001");
    await press("编辑");
    await input("标题", "保留标题");
    vi.mocked(ledgerAction).mockRejectedValue(new V1Error(status, "invalid", "拒绝写入", undefined, "title"));
    await press("保存");
    const title = host.querySelector('[aria-label="标题"]') as HTMLInputElement;
    expect(title.value).toBe("保留标题");
    expect((host.querySelector('button[type="submit"]') as HTMLButtonElement).disabled).toBe(status === 403);
    if (status === 400) expect(title.getAttribute("aria-invalid")).toBe("true");
  });
  it("confirms cancellation inline and edits only source metadata", async () => {
    await render("item=L-0001");
    await press("撤销");
    expect(ledgerAction).not.toHaveBeenCalled();
    await press("确认撤销");
    expect(ledgerAction).toHaveBeenCalledWith({ item: "L-0001", action: "cancel" });
    vi.mocked(ledgerGet).mockResolvedValue({ item: item({ source: { kind: "task", ref: "demo", state: "running", href: "#tasks/demo" } }), events: [] });
    await render("item=L-0002");
    await press("编辑到期、负责人");
    await input("到期", "2026-12-01");
    await press("保存");
    expect(ledgerAction).toHaveBeenLastCalledWith({ item: "L-0001", action: "amend", fields: { due: "2026-12-01" } });
  });
});
