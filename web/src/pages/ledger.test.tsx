// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LedgerPage } from "@/pages/ledger";
import { LedgerSettingsSection } from "@/components/ledger-settings";
import {
  actOnLedgerItem, addLedgerItem, configureLedger, fetchLedgerItem, fetchLedgerItems, fetchLedgerState,
  type LedgerEvent, type LedgerItem, type LedgerState,
} from "@/lib/ledger-api";

vi.mock("@/lib/ledger-api", async (original) => ({
  ...await original<typeof import("@/lib/ledger-api")>(),
  fetchLedgerState: vi.fn(), fetchLedgerItems: vi.fn(), fetchLedgerItem: vi.fn(), actOnLedgerItem: vi.fn(),
  addLedgerItem: vi.fn(), configureLedger: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = "2026-11-19T12:00:00Z";
const item = (n: number, extra: Partial<LedgerItem> = {}): LedgerItem => ({
  id: `L-${String(n).padStart(4, "0")}`, number: n, kind: "pending", status: "open", title: `Item ${n}`, detail: "",
  assurance: "user", source: null, owner: "user", project_id: null, due_at: null, overdue: false, refs: [],
  superseded_by: null, added_via: "dashboard", created_at: NOW, updated_at: NOW, closed_at: null, ...extra,
});
const event = (id: number, extra: Partial<LedgerEvent> = {}): LedgerEvent => ({
  id, action: "add", actor_kind: "user", actor_id: "dashboard", relayed_by: null, via: "dashboard", session_label: null,
  quote: null, note: null, changes: null, created_at: NOW, ...extra,
});
const state = (extra: Partial<LedgerState> = {}): LedgerState => ({
  enabled: true, needs_you: 2, counts: { open: 3, done: 0, cancelled: 0, expired: 0, superseded: 0 },
  operator_unset: false, ...extra,
});

let host: HTMLDivElement;
let root: Root;
let query: string;
const onQuery = vi.fn((next: string) => { query = next; });

const items = (found: LedgerItem[], more = 0) => ({ items: found, more, counts: state().counts });
const flush = () => act(async () => { await Promise.resolve(); });
async function render() {
  await act(async () => root.render(<LedgerPage query={query} onQuery={onQuery} />));
  await flush();
}
const text = () => host.textContent ?? "";
const button = (label: string) => [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === label) as HTMLButtonElement | undefined;

beforeEach(() => {
  query = "";
  onQuery.mockClear();
  vi.mocked(fetchLedgerState).mockResolvedValue(state());
  vi.mocked(fetchLedgerItems).mockImplementation(async (filters) => {
    if (filters.needs_you) return items([
      item(1, { title: "Later one", due_at: "2026-12-30T00:00:00Z" }),
      item(2, { title: "Late one", overdue: true, due_at: "2026-11-10T00:00:00Z" }),
      item(3, { kind: "decision", assurance: "relayed", title: "Use Redis" }),
    ]);
    if (filters.kind === "work_item") return items([item(4, { kind: "work_item", title: "Build it", source: { kind: "task", ref: "task-1", state: "running" }, assurance: "system", owner: "task:task-1" })]);
    if (filters.kind === "decision") return items([item(5, { kind: "decision", title: "Use Postgres", updated_at: new Date().toISOString() }),
      item(6, { kind: "decision", title: "Old decision", updated_at: "2026-01-01T00:00:00Z" })]);
    return items([item(7, { status: "done", title: "Finished" })]);
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe("LedgerPage", () => {
  it("says the ledger is off and where to turn it on, listing nothing", async () => {
    vi.mocked(fetchLedgerState).mockResolvedValue(state({ enabled: false, needs_you: 0 }));
    await render();
    expect(text()).toContain("The ledger is off.");
    expect(host.querySelector('a[href="#settings/chat"]')).not.toBeNull();
    expect(fetchLedgerItems).not.toHaveBeenCalled();
    expect(button("Add")).toBeUndefined();
  });

  it("shows a failed read as a failure with Retry, never as an empty list", async () => {
    vi.mocked(fetchLedgerState).mockRejectedValueOnce(new Error("database unavailable"));
    await render();
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("database unavailable");
    expect(text()).not.toContain("Nothing is waiting for you.");
    await act(async () => button("Retry")!.click());
    await flush();
    expect(text()).toContain("Needs you");
  });

  it("is empty with a plain sentence", async () => {
    vi.mocked(fetchLedgerItems).mockResolvedValue(items([]));
    await render();
    expect(text()).toContain("Nothing is waiting for you.");
  });

  it("puts what needs the user first, overdue before later, relayed decisions marked, and old decisions out", async () => {
    await render();
    const headings = [...host.querySelectorAll("h2")].map((h) => h.textContent?.replace(/\s+/g, " ").trim());
    expect(headings).toEqual(["Needs you (3)", "In progress (1)", "Recent decisions (1)"]);
    const rows = [...host.querySelectorAll("section")[0].querySelectorAll("li")].map((li) => li.textContent ?? "");
    expect(rows[0]).toContain("Late one");
    expect(rows[0]).toMatch(/Overdue/);
    expect(rows[1]).toContain("Later one");
    expect(rows[2]).toContain("Relayed, not confirmed");
    expect(text()).toContain("Use Postgres");
    expect(text()).not.toContain("Old decision");
    expect(text()).toContain("Task task-1");
  });

  it("warns when a chat bot has no My ID", async () => {
    vi.mocked(fetchLedgerState).mockResolvedValue(state({ operator_unset: true }));
    await render();
    expect(text()).toContain("has no My ID set");
  });

  it("keeps the filters in the URL and reads them back", async () => {
    await render();
    const select = host.querySelector("#ledger-status") as HTMLSelectElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(select, "done");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(onQuery).toHaveBeenLastCalledWith("status=done");
    query = "status=done&kind=pending";
    await render();
    expect(fetchLedgerItems).toHaveBeenLastCalledWith({ status: "done", kind: "pending", limit: 50 }, expect.anything());
    expect(text()).toContain("Finished");
    expect((host.querySelector("#ledger-kind") as HTMLSelectElement).value).toBe("pending");
  });

  it("opens an item's history and confirms a relayed decision, showing who said it and how", async () => {
    const relayed = item(3, { kind: "decision", assurance: "relayed", title: "Use Redis" });
    vi.mocked(fetchLedgerItem).mockResolvedValue({ item: relayed, events: [
      event(1, { actor_kind: "relayed", relayed_by: "client:demo", via: "api", quote: "let's use Redis" }),
      event(2, { action: "note", via: "cli", session_label: "tool 2026-11-19 redis", note: "seen" })] });
    vi.mocked(actOnLedgerItem).mockResolvedValue({ ...relayed, assurance: "user" });
    query = "item=L-0003";
    await render();
    const detail = host.querySelector('[aria-label="Details of L-0003"]')!;
    expect(detail.textContent).toContain("Someone else said this");
    expect(detail.textContent).toContain("Relayed · client:demo");
    expect(detail.textContent).toContain("let's use Redis");
    expect(detail.textContent).toContain("Command line · tool 2026-11-19 redis");
    expect(button("Done")).toBeUndefined(); // a decision is not completed
    await act(async () => button("Confirm")!.click());
    expect(actOnLedgerItem).toHaveBeenCalledWith("L-0003", "confirm", {});
  });

  it("gives a sourced item a way to its source and none to change its state here", async () => {
    const sourced = item(4, { kind: "work_item", source: { kind: "task", ref: "task-1", state: "running" }, assurance: "system" });
    vi.mocked(fetchLedgerItem).mockResolvedValue({ item: sourced, events: [event(1, { actor_kind: "system", via: "system" })] });
    query = "item=L-0004";
    await render();
    expect(host.querySelector('a[href="#tasks/task-1"]')).not.toBeNull();
    for (const label of ["Done", "Cancel it", "Confirm", "Reopen"]) expect(button(label)).toBeUndefined();
    expect(button("Edit")).toBeDefined();
  });

  it("closes an own to-do, adds a note, and shows the server's refusal", async () => {
    vi.mocked(fetchLedgerItem).mockResolvedValue({ item: item(1), events: [event(1)] });
    vi.mocked(actOnLedgerItem).mockResolvedValueOnce(item(1, { status: "done" }));
    query = "item=L-0001";
    await render();
    await act(async () => button("Done")!.click());
    expect(actOnLedgerItem).toHaveBeenCalledWith("L-0001", "close", {});
    const input = host.querySelector('input[placeholder="Add a note"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "sent it");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    vi.mocked(actOnLedgerItem).mockRejectedValueOnce(new Error("L-0001 is done, not open"));
    await act(async () => button("Note")!.click());
    expect(actOnLedgerItem).toHaveBeenLastCalledWith("L-0001", "note", { note: "sent it" });
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("is done, not open");
  });

  it("adds an item as the user", async () => {
    vi.mocked(addLedgerItem).mockResolvedValue(item(8));
    await render();
    await act(async () => button("Add")!.click());
    const title = host.querySelector('input[maxlength="200"]') as HTMLInputElement;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(title, "Call the vendor");
      title.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
    expect(addLedgerItem).toHaveBeenCalledWith({ kind: "pending", title: "Call the vendor" });
  });
});

describe("LedgerSettingsSection", () => {
  it("turns the ledger on and off, and shows a failure", async () => {
    vi.mocked(fetchLedgerState).mockResolvedValue(state({ enabled: false }));
    vi.mocked(configureLedger).mockResolvedValueOnce(state({ enabled: true, operator_unset: true }));
    await act(async () => root.render(<LedgerSettingsSection />));
    await flush();
    const box = host.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(box.checked).toBe(false);
    await act(async () => box.click());
    expect(configureLedger).toHaveBeenCalledWith(true);
    expect(text()).toContain("has no My ID set yet");
    vi.mocked(configureLedger).mockRejectedValueOnce(new Error("could not save"));
    await act(async () => (host.querySelector('input[type="checkbox"]') as HTMLInputElement).click());
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("could not save");
  });
});
