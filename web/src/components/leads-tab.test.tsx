// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LeadsTab } from "@/components/leads-tab";
import { memoryProposeFromLead, memorySettingsGet, type MemoryDetail } from "@/lib/memory";
import { V1Error } from "@/lib/v1";
import {
  leadsChats, leadsDelete, leadsMark, leadsSearch, leadsSetChat, type Lead,
} from "@/lib/api-leads";

vi.mock("@/lib/api-leads", () => ({
  leadsSearch: vi.fn(), leadsMark: vi.fn(), leadsDelete: vi.fn(), leadsChats: vi.fn(), leadsSetChat: vi.fn(),
}));
vi.mock("@/lib/memory", () => ({ memorySettingsGet: vi.fn(), memoryProposeFromLead: vi.fn() }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const source = {
  chat: "设计群", chat_id: "oc_design", chat_kind: "group", sender: "Ada", sender_id: "ou_ada",
  sent_at: "2026-11-19T08:00:00Z", message_id: "om_1", link: "https://example.test/m/1",
};
const lead: Lead = {
  id: "lead-1", kind: "decision", summary: "下周改口径", detail: "详细说明", confidence: "high", state: "active",
  valid_until: "2026-12-01T00:00:00Z", superseded_by: "lead-0", sources: [source], sources_total: 4,
};

describe("Leads tab", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.mocked(memorySettingsGet).mockResolvedValue({ configured: true, enabled: true, settings: { sources: { manager: false, tasks: false, leads: true } }, problem: null });
    vi.mocked(memoryProposeFromLead).mockResolvedValue({ id: "M-0042", state: "drafting" } as MemoryDetail);
    vi.mocked(leadsSearch).mockResolvedValue({
      notice: "来自飞书消息，未经确认；只当线索，不当权威。", leads: [lead], more: 0,
    });
    vi.mocked(leadsChats).mockResolvedValue({ source_id: "lark-user", chats: [], total: 0 });
    vi.mocked(leadsMark).mockResolvedValue({ lead: { ...lead, state: "inaccurate" } });
    vi.mocked(leadsDelete).mockResolvedValue({ deleted: "lead-1" });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.clearAllMocks();
  });

  const show = async () => { await act(async () => { root.render(<LeadsTab />); }); };
  const button = (rootNode: ParentNode, text: string) =>
    [...rootNode.querySelectorAll("button")].find((item) => item.textContent === text);
  const setValue = (el: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement, value: string) => {
    const prototype = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  };

  it("searches with the filters it shows", async () => {
    await show();
    expect(container.textContent).toContain("来自飞书消息，未经确认；只当线索，不当权威。");
    expect(container.textContent).toContain("决定");
    expect(container.textContent).toContain("有效至 2026-12-01");
    expect(container.textContent).toContain("已被推翻 → lead-0");
    expect(leadsSearch).toHaveBeenCalledWith({ state: "active" });
    await act(async () => {
      setValue(container.querySelector('input[aria-label="搜索"]')!, "口径");
      setValue(container.querySelector('input[aria-label="会话"]')!, "设计群");
      setValue(container.querySelector('input[aria-label="人"]')!, "Ada");
      setValue(container.querySelector('input[aria-label="天数"]')!, "3");
      setValue(container.querySelector('select[aria-label="种类"]')!, "decision");
      setValue(container.querySelector('select[aria-label="状态"]')!, "expired");
    });
    expect(leadsSearch).toHaveBeenLastCalledWith({
      state: "expired", query: "口径", chat: "设计群", person: "Ada", days: 3, kind: "decision",
    });
    expect(container.textContent).not.toContain("另有");
  });

  it("shows how many leads remain, and never the zero", async () => {
    await show();
    expect(container.textContent).not.toContain("另有");
    vi.mocked(leadsSearch).mockResolvedValue({
      notice: "来自飞书消息，未经确认；只当线索，不当权威。", leads: [lead], more: 5,
    });
    await act(async () => setValue(container.querySelector('input[aria-label="搜索"]')!, "口径"));
    expect(container.textContent).toContain("另有 5 条");
  });

  it("marks a lead inaccurate with a note, and deletes one after a confirm", async () => {
    await show();
    await act(async () => button(container, "标为不准确")!.click());
    const mark = container.querySelector("[role=dialog]")!;
    await act(async () => setValue(mark.querySelector("textarea")!, "口径已经变了"));
    await act(async () => button(mark, "标为不准确")!.click());
    expect(leadsMark).toHaveBeenCalledWith({ lead_id: "lead-1", note: "口径已经变了" });
    await act(async () => button(container, "删除")!.click());
    const remove = container.querySelector("[role=dialog]")!;
    await act(async () => button(remove, "删除")!.click());
    expect(leadsDelete).toHaveBeenCalledWith({ lead_id: "lead-1" });
  });

  it("asks before excluding a chat and sends the row's delete counts", async () => {
    vi.mocked(leadsChats).mockResolvedValue({ source_id: "lark-user", total: 1, chats: [{
      chat_id: "oc_design", name: "设计群", kind: "group", extraction: "included", extraction_excluded_at: null,
      last_message_at: "2026-11-19T08:00:00Z", leads: 3, open_intents: 2,
    }] });
    vi.mocked(leadsSetChat).mockResolvedValue({ chat_id: "oc_design", extraction: "excluded", deleted_leads: 3, deleted_intents: 2 });
    await show();
    const toggle = container.querySelector<HTMLButtonElement>('[role="switch"][aria-label="设计群 整理"]')!;
    await act(async () => toggle.click());
    expect(leadsSetChat).not.toHaveBeenCalled();
    const dialog = container.querySelector("[role=dialog]")!;
    expect(dialog.textContent).toContain("待处理意图 2 条（以及其他未建任务的意图）");
    expect(dialog.textContent).toContain("3 条线索");
    await act(async () => dialog.querySelector("input")!.click());
    await act(async () => button(dialog, "确认")!.click());
    expect(leadsSetChat).toHaveBeenCalledWith({ chat_id: "oc_design", extraction: "excluded", delete_extracted: true });
    expect(container.textContent).toContain("已删除 3 条线索、2 条意图。");
    expect(container.textContent).not.toContain("已改回待处理");
  });

  it("says when merged intents from other chats were put back to pending", async () => {
    vi.mocked(leadsChats).mockResolvedValue({ source_id: "lark-user", total: 1, chats: [{
      chat_id: "oc_design", name: "设计群", kind: "group", extraction: "included", extraction_excluded_at: null,
      last_message_at: "2026-11-19T08:00:00Z", leads: 3, open_intents: 2,
    }] });
    vi.mocked(leadsSetChat).mockResolvedValue({
      chat_id: "oc_design", extraction: "excluded", deleted_leads: 3, deleted_intents: 2, detached_intents: 4,
    });
    await show();
    await act(async () => container.querySelector<HTMLButtonElement>('[role="switch"][aria-label="设计群 整理"]')!.click());
    const dialog = container.querySelector("[role=dialog]")!;
    await act(async () => dialog.querySelector("input")!.click());
    await act(async () => button(dialog, "确认")!.click());
    expect(container.textContent).toContain("已删除 3 条线索、2 条意图。");
    expect(container.textContent).toContain("另有 4 条合并进来的意图已改回待处理");
  });

  it.each([
    { configured: false, enabled: false, settings: null, problem: null },
    { configured: true, enabled: false, settings: { sources: { manager: false, tasks: false, leads: true } }, problem: null },
    { configured: true, enabled: true, settings: { sources: { manager: false, tasks: false, leads: false } }, problem: null },
  ])("hides memory proposals when their source is off: %j", async (view) => {
    vi.mocked(memorySettingsGet).mockResolvedValue(view);
    await show();
    expect(button(container, "提议记入记忆")).toBeUndefined();
    expect(memoryProposeFromLead).not.toHaveBeenCalled();
  });

  it.each(["decision", "commitment", "fact"])("offers an active or expired %s lead with its mapped kind", async (kind) => {
    vi.mocked(leadsSearch).mockResolvedValue({ notice: "", leads: [{ ...lead, kind, state: "expired" }], more: 0 });
    await show();
    await act(async () => button(container, "提议记入记忆")!.click());
    const dialog = container.querySelector('[role="dialog"]')!;
    expect(dialog.textContent).toContain("来自飞书消息，未经确认：写入前你还要确认一次");
    const select = dialog.querySelector("select")!;
    expect(select.value).toBe(kind);
    expect([...select.options].map((option) => option.value)).toEqual(["decision", "commitment", "fact", "person", "project"]);
    await act(async () => button(dialog, "取消")!.click());
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    expect(memoryProposeFromLead).not.toHaveBeenCalled();
  });

  it.each([
    ["request", "active"], ["status", "active"], ["issue", "active"],
    ["decision", "superseded"], ["decision", "inaccurate"], ["decision", "dropped"], ["decision", "promoted"],
  ])("hides memory proposals for %s in %s", async (kind, state) => {
    vi.mocked(leadsSearch).mockResolvedValue({ notice: "", leads: [{ ...lead, kind, state }], more: 0 });
    await show();
    expect(button(container, "提议记入记忆")).toBeUndefined();
  });

  it("proposes a chosen external kind and links to memory confirmation", async () => {
    await show();
    await act(async () => button(container, "提议记入记忆")!.click());
    const dialog = container.querySelector('[role="dialog"]')!;
    await act(async () => setValue(dialog.querySelector("select")!, "person"));
    await act(async () => button(dialog, "提议")!.click());
    expect(memoryProposeFromLead).toHaveBeenCalledExactlyOnceWith("lead-1", "person");
    expect(container.querySelector('[role="dialog"]')).toBeNull();
    const link = container.querySelector<HTMLAnchorElement>('a[href="#/memory"]')!;
    expect(link.textContent).toBe("已提议 M-0042，去记忆页确认");
    expect(memorySettingsGet).toHaveBeenCalledTimes(1);
  });

  it.each([
    [new V1Error(409, "conflict", "lead-1 is already proposed as M-0042"), '[data-state="conflict"]'],
    [new V1Error(400, "invalid", "kind person has no target in the memory_write setting"), '[role="alert"]'],
  ])("shows a refusal without claiming a proposal succeeded", async (error, selector) => {
    vi.mocked(memoryProposeFromLead).mockRejectedValue(error);
    await show();
    await act(async () => button(container, "提议记入记忆")!.click());
    await act(async () => button(container.querySelector('[role="dialog"]')!, "提议")!.click());
    expect(container.querySelector('[role="dialog"]')?.querySelector(selector)?.textContent).toContain(error.message);
    expect(container.querySelector('a[href="#/memory"]')).toBeNull();
    expect(container.querySelector('[role="dialog"]')).not.toBeNull();
  });

});
