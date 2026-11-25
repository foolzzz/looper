// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Sidebar } from "@/components/sidebar";
import { WaitingGatesProvider } from "@/lib/use-waiting-gates";
import { fetchGates, fetchOverview, fetchProjectsList, type GateList } from "@/lib/api";
import { intentsSettingsGet } from "@/lib/api-leads";
import { attentionSettingsGet, fetchAttention, type AttentionSummary } from "@/lib/api-attention";
import { V1Error } from "@/lib/v1";
import { useLedger } from "@/lib/use-ledger";
import { taskConversationsGet } from "@/lib/work";

vi.mock("@/lib/api", async (original) => ({
  ...await original<typeof import("@/lib/api")>(),
  fetchGates: vi.fn(), fetchProjectsList: vi.fn(), fetchOverview: vi.fn(),
}));
vi.mock("@/lib/api-leads", () => ({ intentsSettingsGet: vi.fn() }));
vi.mock("@/lib/api-attention", () => ({ attentionSettingsGet: vi.fn(), fetchAttention: vi.fn(),
  isAttentionOff: (err: unknown) => err instanceof V1Error && err.status === 409 }));
vi.mock("@/lib/use-ledger", () => ({ useLedger: vi.fn() }));
vi.mock("@/components/sign-out", () => ({ SignOut: () => null }));
vi.mock("@/components/project-dialog", () => ({ ProjectDialog: () => <div role="dialog" /> }));
vi.mock("@/lib/work", async (original) => ({
  ...await original<typeof import("@/lib/work")>(), taskConversationsGet: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const gates = (waiting: number): GateList => ({
  gates: [], total: 0, limit: 1, offset: 0, next_cursor: null, has_more: false,
  counts: { open: waiting, waiting_user: waiting, waiting_planner: 0, closed: 0, all: waiting },
});

let root: Root;
let host: HTMLElement;
const onNavigate = vi.fn();

let activePage: "gates" | "dashboard" = "gates";

async function render() {
  await act(async () => {
    root.render(
      <WaitingGatesProvider>
        <Sidebar activePage={activePage} activeSettingsPage={null} activeProjectId={null} onNavigate={onNavigate}
          onOpenProject={vi.fn()} onOpenSettings={vi.fn()} open onClose={vi.fn()} />
      </WaitingGatesProvider>,
    );
  });
}
const attentionSummary = (total: number): AttentionSummary => ({
  read_at: "2026-11-24T00:00:00Z", total,
  sections: [{ kind: "gates", state: "ok", count: total, items: [], error: null }],
});
const todoButton = () => [...host.querySelectorAll("nav button")].find((b) => b.textContent?.includes("Needs you"));
const todoBadge = () => todoButton()?.querySelector("[aria-label]");
const gatesButton = () => [...host.querySelectorAll("nav button")].find((b) => b.textContent?.includes("User Gates"))!;

beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(fetchProjectsList).mockResolvedValue({ projects: [], projects_root: "" });
  vi.mocked(fetchGates).mockResolvedValue(gates(2));
  vi.mocked(intentsSettingsGet).mockResolvedValue({ enabled: false, subscriptions: [] });
  vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: false });
  vi.mocked(taskConversationsGet).mockResolvedValue({ enabled: false });
  vi.mocked(fetchAttention).mockRejectedValue(new Error("summary is off"));
  vi.mocked(useLedger).mockReturnValue({ enabled: false, count: null, stale: false, error: null,
    revision: 0, refresh: vi.fn(), changed: vi.fn() });
  onNavigate.mockClear();
  vi.mocked(fetchOverview).mockResolvedValue({} as never);
  activePage = "gates";
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("Sidebar User Gates item", () => {
  it("sits between Tasks and Schedules, is active and navigates", async () => {
    await render();
    const labels = [...host.querySelectorAll("nav")[0].querySelectorAll("button")].map((b) => b.textContent ?? "");
    const at = (name: string) => labels.findIndex((l) => l.includes(name));
    expect(at("Tasks")).toBeGreaterThanOrEqual(0);
    expect(at("User Gates")).toBe(at("Tasks") + 1);
    expect(at("Schedules")).toBe(at("User Gates") + 1);
    expect(labels.some((label) => label.includes("Runs"))).toBe(false);
    expect(labels.some((label) => label.includes("Intents"))).toBe(false);
    expect(gatesButton().className).toContain("text-primary");
    act(() => (gatesButton() as HTMLButtonElement).click());
    expect(onNavigate).toHaveBeenCalledWith("gates");
  });

  it("shows the waiting count as a badge", async () => {
    await render();
    expect(host.querySelector('[aria-label="2 gates waiting for you"]')?.textContent).toBe("2");
  });

  it("uses the singular for one", async () => {
    vi.mocked(fetchGates).mockResolvedValue(gates(1));
    await render();
    expect(host.querySelector('[aria-label="1 gate waiting for you"]')?.textContent).toBe("1");
  });

  it("caps the badge at 99+", async () => {
    vi.mocked(fetchGates).mockResolvedValue(gates(150));
    await render();
    expect(host.querySelector('[aria-label="150 gates waiting for you"]')?.textContent).toBe("99+");
  });

  it("shows no badge for 0", async () => {
    vi.mocked(fetchGates).mockResolvedValue(gates(0));
    await render();
    expect(host.querySelector('[aria-label$="waiting for you"]')).toBeNull();
  });

  it("shows no badge when the fetch fails", async () => {
    vi.mocked(fetchGates).mockRejectedValue(new Error("down"));
    await render();
    expect(host.querySelector('[aria-label$="waiting for you"]')).toBeNull();
  });

  it("polls every 10 s, reloads on the gates event and stops after unmount", async () => {
    await render();
    expect(fetchGates).toHaveBeenCalledWith({ state: "open", waitingOn: "user", limit: 1 });
    expect(fetchGates).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(fetchGates).toHaveBeenCalledTimes(2);
    await act(async () => { window.dispatchEvent(new Event("looper:gates-changed")); });
    expect(fetchGates).toHaveBeenCalledTimes(3);
    act(() => root.unmount());
    await vi.advanceTimersByTimeAsync(30_000);
    window.dispatchEvent(new Event("looper:gates-changed"));
    expect(fetchGates).toHaveBeenCalledTimes(3);
    root = createRoot(host);
  });
});


describe("Sidebar Intents item", () => {
  it("appears only when intent recognition is on, and opens that page", async () => {
    await render();
    expect(host.textContent).not.toContain("Intents");
    vi.mocked(intentsSettingsGet).mockResolvedValue({ enabled: true, subscriptions: [] });
    act(() => root.unmount());
    root = createRoot(host);
    await render();
    const button = [...host.querySelectorAll("nav button")].find((item) => item.textContent?.includes("Intents"))!;
    expect(button.textContent).toContain("意图");
    act(() => (button as HTMLButtonElement).click());
    expect(onNavigate).toHaveBeenCalledWith("intents");
  });
});

describe("Sidebar Needs you item", () => {
  it("skips the summary while off, and opens the enabled page", async () => {
    await render();
    expect(host.textContent).not.toContain("Needs you");
    expect(attentionSettingsGet).toHaveBeenCalledTimes(1);
    expect(fetchAttention).not.toHaveBeenCalled();
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    vi.mocked(fetchAttention).mockResolvedValue(attentionSummary(0));
    act(() => root.unmount());
    root = createRoot(host);
    await render();
    const labels = [...host.querySelectorAll("nav")[0].querySelectorAll("button")].map((b) => b.textContent ?? "");
    expect(labels.slice(0, 3)).toEqual(["Dashboard", "Needs you", "Chat"]);
    expect(fetchAttention).toHaveBeenCalledTimes(1);
    const button = [...host.querySelectorAll("nav button")].find((item) => item.textContent === "Needs you")!;
    act(() => (button as HTMLButtonElement).click());
    expect(onNavigate).toHaveBeenCalledWith("todo");
  });

  it.each([0, 1, 7, 150])("shows the summary total %s, hiding zero and capping large counts", async (total) => {
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    vi.mocked(fetchAttention).mockResolvedValue(attentionSummary(total));
    await render();
    if (total === 0) expect(todoBadge()).toBeNull();
    else {
      expect(todoBadge()?.textContent).toBe(total > 99 ? "99+" : String(total));
      expect(todoBadge()?.getAttribute("aria-label")).toBe(`${total} 项待你处理`);
    }
  });

  it("shows an unknown count while loading or when the summary fails", async () => {
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    let reject!: (err: Error) => void;
    vi.mocked(fetchAttention).mockReturnValue(new Promise((_, fail) => { reject = fail; }));
    await render();
    expect(todoBadge()?.textContent).toBe("?");
    await act(async () => { reject(new Error("unavailable")); });
    expect(todoBadge()?.textContent).toBe("?");
  });

  it.each(["error", "unknown"] as const)("shows ? for an incomplete summary with a %s section", async (state) => {
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    const summary = attentionSummary(3);
    summary.sections.push({ kind: "logins", state, count: 0, items: [], error: null });
    vi.mocked(fetchAttention).mockResolvedValue(summary);
    await render();
    expect(todoBadge()?.textContent).toBe("?");
  });

  it("refreshes every 30 s, clears failed counts, recovers and observes the switch", async () => {
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    vi.mocked(fetchAttention).mockResolvedValue(attentionSummary(3));
    await render();
    expect(todoBadge()?.textContent).toBe("3");
    vi.mocked(fetchAttention).mockRejectedValue(new Error("down"));
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(todoBadge()?.textContent).toBe("?");
    vi.mocked(fetchAttention).mockResolvedValue(attentionSummary(7));
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(todoBadge()?.textContent).toBe("7");
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(todoButton()).toBeUndefined();
    expect(fetchAttention).toHaveBeenCalledTimes(3);
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(todoBadge()?.textContent).toBe("7");
    const calls = vi.mocked(fetchAttention).mock.calls.length;
    act(() => root.unmount());
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetchAttention).toHaveBeenCalledTimes(calls);
    root = createRoot(host);
  });

  it("aborts the pending summary on unmount and ignores a late setting read", async () => {
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    vi.mocked(fetchAttention).mockReturnValue(new Promise(() => {}));
    await render();
    const signal = vi.mocked(fetchAttention).mock.calls[0][0]!;
    act(() => root.unmount());
    expect(signal.aborted).toBe(true);
    root = createRoot(host);
    let finish!: (settings: { enabled: boolean }) => void;
    vi.mocked(attentionSettingsGet).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    await render();
    act(() => root.unmount());
    await act(async () => { finish({ enabled: true }); });
    expect(fetchAttention).toHaveBeenCalledTimes(1);
    root = createRoot(host);
  });

  it("hides the entry if the switch turns off between the setting and summary reads", async () => {
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    vi.mocked(fetchAttention).mockRejectedValue(new V1Error(409, "conflict", "off"));
    await render();
    expect(todoButton()).toBeUndefined();
  });

  it("keeps the page hidden when its setting cannot be read", async () => {
    vi.mocked(attentionSettingsGet).mockRejectedValue(new Error("unavailable"));
    await render();
    expect(host.textContent).not.toContain("Needs you");
    expect(fetchAttention).not.toHaveBeenCalled();
  });
});

describe("Sidebar Work item", () => {
  const workButton = () => [...host.querySelectorAll("nav button")].find((b) => b.textContent === "Work");

  it("stops probing a missing operation after the first request", async () => {
    vi.mocked(taskConversationsGet).mockRejectedValue(
      new V1Error(404, "not_found", "no POST /api/v1/task_conversations/get"),
    );
    await render();
    expect(taskConversationsGet).toHaveBeenCalledTimes(1);
    expect(workButton()).toBeUndefined();
    await act(async () => { await vi.advanceTimersByTimeAsync(150_000); });
    expect(taskConversationsGet).toHaveBeenCalledTimes(1);
    expect(workButton()).toBeUndefined();
  });

  it("keeps polling while disabled and observes the switch turning on", async () => {
    await render();
    expect(taskConversationsGet).toHaveBeenCalledTimes(1);
    expect(workButton()).toBeUndefined();
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(taskConversationsGet).toHaveBeenCalledTimes(4);
    expect(workButton()).toBeUndefined();
    vi.mocked(taskConversationsGet).mockResolvedValue({ enabled: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(workButton()).toBeDefined();
  });

  it("shows Work when enabled", async () => {
    vi.mocked(taskConversationsGet).mockResolvedValue({ enabled: true });
    await render();
    expect(workButton()).toBeDefined();
  });

  it("hides Work after a server error and keeps polling to recover", async () => {
    vi.mocked(taskConversationsGet).mockResolvedValue({ enabled: true });
    await render();
    expect(workButton()).toBeDefined();
    vi.mocked(taskConversationsGet).mockRejectedValue(new V1Error(500, "internal", "unavailable"));
    await act(async () => { await vi.advanceTimersByTimeAsync(90_000); });
    expect(taskConversationsGet).toHaveBeenCalledTimes(4);
    expect(workButton()).toBeUndefined();
    vi.mocked(taskConversationsGet).mockResolvedValue({ enabled: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(workButton()).toBeDefined();
  });
});

describe("Sidebar version label", () => {
  it("shows the overview's version next to the brand", async () => {
    vi.mocked(fetchOverview).mockResolvedValue({ version: "2.13.1" } as never);
    await render();
    const label = host.querySelector('[title="2.13.1"]');
    expect(label?.textContent).toBe("v2.13.1");
    expect(label?.className).toContain("whitespace-nowrap");
  });

  it("shows nothing when the overview has no version", async () => {
    vi.mocked(fetchOverview).mockResolvedValue({} as never);
    await render();
    expect(host.textContent).toContain("Looper");
    expect(host.textContent).not.toMatch(/v\d/);
  });

  it("shows nothing while loading", async () => {
    vi.mocked(fetchOverview).mockReturnValue(new Promise(() => {}));
    await render();
    expect(host.textContent).not.toMatch(/v\d/);
  });
});

describe("Sidebar New project dialog", () => {
  it("opens on document.body, outside the sidebar that would confine it", async () => {
    await render();
    await act(async () => {
      host.querySelector<HTMLButtonElement>('button[aria-label="New project"]')!.click();
    });
    const dialog = document.querySelector('[role="dialog"]')!;
    expect(dialog.parentElement).toBe(document.body);
    expect(host.querySelector('[role="dialog"]')).toBeNull();
  });
});

describe("Sidebar Memory item", () => {
  it("is always listed after the Message stream and opens its page", async () => {
    await render();
    const labels = [...host.querySelectorAll("nav")[0].querySelectorAll("button")].map((b) => b.textContent ?? "");
    expect(labels.findIndex((l) => l.includes("Memory"))).toBe(labels.findIndex((l) => l.includes("Message stream")) + 1);
    const button = [...host.querySelectorAll("nav button")].find((item) => item.textContent?.includes("Memory"))!;
    act(() => (button as HTMLButtonElement).click());
    expect(onNavigate).toHaveBeenCalledWith("memory");
  });
});

it("shows the ledger after Tasks only when enabled with its actual waiting count", async () => {
  await render();
  expect(host.textContent).not.toContain("Ledger 账本");
  for (const count of [0, 3, 150, null]) {
    vi.mocked(useLedger).mockReturnValue({ enabled: true, count, stale: false, error: null,
      revision: 0, refresh: vi.fn(), changed: vi.fn() });
    await render();
    const buttons = [...host.querySelectorAll("nav")[0].querySelectorAll("button")];
    const index = buttons.findIndex((b) => b.textContent?.includes("Ledger 账本"));
    expect(index).toBe(buttons.findIndex((b) => b.textContent === "Tasks") + 1);
    const badge = buttons[index].querySelector("[aria-label]");
    if (count === 0) expect(badge).toBeNull();
    else if (count === null) expect(buttons[index].textContent).toContain("?");
    else {
      expect(badge?.getAttribute("aria-label")).toBe(`${count} 项待你处理`);
      expect(badge?.textContent).toBe(count > 99 ? "99+" : String(count));
    }
    act(() => (buttons[index] as HTMLButtonElement).click());
    expect(onNavigate).toHaveBeenLastCalledWith("ledger");
  }
  vi.mocked(useLedger).mockReturnValue({ enabled: true, count: 3, stale: true, error: "unavailable",
    revision: 0, refresh: vi.fn(), changed: vi.fn() });
  await render();
  expect(host.textContent).toContain("暂无法刷新");
  expect(host.querySelector('[aria-label="3 项待你处理"]')?.textContent).toBe("3");
});
