// @vitest-environment happy-dom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkAttention } from "@/components/work-attention";
import { attentionSettingsGet, fetchAttention, type AttentionItem, type AttentionSection, type AttentionSummary } from "@/lib/api-attention";
import { useProjects } from "@/lib/use-projects";
import * as v1 from "@/lib/v1";
import { V1Error } from "@/lib/v1";
import { WorkPage } from "@/pages/work";

vi.mock("@/lib/api-attention", async (original) => ({
  ...await original<typeof import("@/lib/api-attention")>(),
  attentionSettingsGet: vi.fn(),
  fetchAttention: vi.fn(),
}));
vi.mock("@/lib/use-projects", () => ({
  useProjects: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const item = (partial: Partial<AttentionItem> & Pick<AttentionItem, "key" | "title">): AttentionItem => ({
  context: "", since: null, href: null, ref: null, facts: {}, ...partial,
});

const section = (
  partial: Partial<AttentionSection> & Pick<AttentionSection, "kind" | "state">,
): AttentionSection => ({
  count: partial.items?.length ?? 0, error: null, items: [], ...partial,
});

const summary = (sections: AttentionSection[], total = sections.reduce((sum, part) => sum + (part.state === "ok" ? part.count : 0), 0)): AttentionSummary => ({
  read_at: "2026-11-20T08:00:00Z", total, sections,
});

let root: Root;
let host: HTMLElement;

function visibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
}

async function render(node: ReactNode = <WorkAttention />) {
  await act(async () => { root.render(node); });
}

beforeEach(() => {
  visibility("visible");
  vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
  vi.mocked(fetchAttention).mockResolvedValue(summary([]));
  vi.mocked(useProjects).mockReturnValue({ projects: [], projectsRoot: "", error: "", reload: vi.fn() });
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Work attention settings", () => {
  it("reads the summary when the setting is on", async () => {
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({ kind: "gates", state: "ok", count: 1, items: [item({ key: "g1", title: "Pick a database", href: "#tasks/task-1" })] }),
    ]));
    await render();
    expect(attentionSettingsGet).toHaveBeenCalledTimes(1);
    expect(vi.mocked(attentionSettingsGet).mock.calls[0]?.[0]).toBeInstanceOf(AbortSignal);
    expect(fetchAttention).toHaveBeenCalledTimes(1);
    expect(vi.mocked(fetchAttention).mock.calls[0]?.[0]).toBeInstanceOf(AbortSignal);
    expect(host.querySelector("h2")?.textContent).toBe("需要你处理");
    expect(host.textContent).toContain("Pick a database");
    expect(host.textContent).not.toContain("待办汇总未开启");
  });

  it("shows that the summary is off and does not read it or send a write", async () => {
    const call = vi.spyOn(v1, "callV1");
    const confirm = vi.spyOn(v1, "confirmV1");
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: false });
    await render();
    expect(host.textContent).toContain("待办汇总未开启");
    expect(fetchAttention).not.toHaveBeenCalled();
    expect(call).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    expect(host.querySelector("button")).toBeNull();
    expect(host.textContent).not.toContain("暂时没有需要你处理的事");
    call.mockRestore();
    confirm.mockRestore();
  });

  it("says the server has no summary when the settings operation is missing", async () => {
    vi.mocked(attentionSettingsGet).mockRejectedValue(new V1Error(404, "not_found", "no POST /api/v1/attention/settings_get"));
    await render();
    expect(host.textContent).toContain("此服务器尚不支持待办汇总");
    expect(fetchAttention).not.toHaveBeenCalled();
    expect(host.textContent).not.toContain("暂时没有需要你处理的事");
    expect(host.textContent).not.toContain("待办读取失败");
  });

  it("says the server has no summary when the summary operation is missing", async () => {
    vi.mocked(fetchAttention).mockRejectedValue(new V1Error(404, "not_found", "no POST /api/v1/attention/summary"));
    await render();
    expect(host.textContent).toContain("此服务器尚不支持待办汇总");
    expect(host.textContent).not.toContain("待办读取失败");
  });

  it("shows a failed read and its reason, then retries the read", async () => {
    vi.mocked(attentionSettingsGet).mockRejectedValueOnce(new Error("network down"));
    await render();
    const alert = host.querySelector('[role="alert"]');
    expect(alert?.textContent).toContain("待办读取失败");
    expect(alert?.textContent).toContain("network down");
    expect(fetchAttention).not.toHaveBeenCalled();
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: true });
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({ kind: "gates", state: "ok", count: 1, items: [item({ key: "g1", title: "Pick a database" })] }),
    ]));
    await act(async () => { (alert?.querySelector("button") as HTMLButtonElement).click(); });
    expect(host.textContent).toContain("Pick a database");
    expect(host.textContent).not.toContain("待办读取失败");
  });

  it("treats a disabled summary response as off, not as an empty list or a generic failure", async () => {
    vi.mocked(fetchAttention).mockRejectedValue(new V1Error(409, "conflict", "not enabled"));
    await render();
    expect(host.textContent).toContain("待办汇总未开启");
    expect(host.textContent).not.toContain("待办读取失败");
    expect(host.textContent).not.toContain("暂时没有需要你处理的事");
  });
});

describe("Work attention sections", () => {
  it("says nothing needs attention only when every source is empty and ok", async () => {
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({ kind: "gates", state: "ok", count: 0, items: [] }),
      section({ kind: "ledger", state: "ok", count: 0, items: [] }),
    ], 0));
    await render();
    expect(host.textContent).toContain("暂时没有需要你处理的事");
    expect(host.textContent).toContain("没有待办");
    const labels = [...host.querySelectorAll("section")].map((node) => node.getAttribute("aria-label"));
    expect(labels).toEqual(["Gates waiting for your answer", "Ledger items waiting for you"]);
  });

  it("keeps an empty source list from claiming that nothing needs attention", async () => {
    vi.mocked(fetchAttention).mockResolvedValue(summary([], 0));
    await render();
    expect(host.textContent).not.toContain("暂时没有需要你处理的事");
    expect(host.textContent).toContain("需要你处理");
  });

  it("shows readable items beside a failed source and an unknown source, in server order", async () => {
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({ kind: "ledger", state: "ok", count: 1, items: [item({
        key: "ledger:3", title: "Write the report", context: "Weekly", href: "#ledger", ref: "L-0003",
        facts: { kind: "work_item", overdue: true },
      })] }),
      section({ kind: "deliveries", state: "error", count: 0, error: "database down", items: [] }),
      section({ kind: "confirmations", state: "unknown", count: 0, items: [] }),
      section({ kind: "gates", state: "ok", count: 0, items: [] }),
    ], 1));
    await render();
    const labels = [...host.querySelectorAll("section")].map((node) => node.getAttribute("aria-label"));
    expect(labels).toEqual([
      "Ledger items waiting for you",
      "Messages that may not have arrived",
      "Operations to confirm",
      "Gates waiting for your answer",
    ]);
    expect(host.textContent).toContain("Write the report");
    expect(host.textContent).toContain("Weekly");
    expect(host.textContent).toContain("L-0003");
    expect(host.textContent).toContain("overdue");
    const failed = host.querySelector('section[aria-label="Messages that may not have arrived"]');
    expect(failed?.textContent).toContain("database down");
    expect(failed?.querySelector("button")?.textContent).toBe("重试");
    expect(host.textContent).toContain("此来源状态未知");
    expect(host.querySelector('section[aria-label="Gates waiting for your answer"]')?.textContent).toContain("没有待办");
    expect(host.textContent).not.toContain("暂时没有需要你处理的事");
    expect(host.textContent).toContain("Write the report");
  });

  it("keeps the last readable sections when a later read fails", async () => {
    vi.useFakeTimers();
    vi.mocked(fetchAttention).mockResolvedValueOnce(summary([
      section({ kind: "gates", state: "ok", count: 1, items: [item({ key: "g1", title: "Pick a database" })] }),
    ])).mockRejectedValueOnce(new Error("network down"));
    await render();
    expect(host.textContent).toContain("Pick a database");
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(host.textContent).toContain("待办读取失败");
    expect(host.textContent).toContain("network down");
    expect(host.textContent).toContain("Pick a database");
  });
});

describe("Work attention links and actions", () => {
  const paused = summary([
    section({ kind: "schedules", state: "ok", count: 1, items: [item({
      key: "schedule:sched-paused", title: "Morning brief", context: "Demo", href: "#schedules/sched-paused",
      facts: { alerts: ["unanswered"], failures: 2 },
    })] }),
    section({ kind: "confirmations", state: "ok", count: 1, items: [item({
      key: "confirm:1", title: "Approve the send", context: "Chat", href: null,
      facts: { channel: "web", expires_in_seconds: 120 },
    })] }),
    section({ kind: "gates", state: "ok", count: 1, items: [item({
      key: "gate:1", title: "See #tasks/guessed-from-text", context: "Login rewrite", href: "#tasks/task-1",
      facts: { step: "s2", questions: 1, token: "super-secret-token" },
    })] }),
    section({ kind: "cost", state: "ok", count: 1, items: [item({
      key: "cost:1", title: "Spend is waiting", href: "#settings/cost",
      facts: { currency: "provider_priced_usd", spent_usd: 2, limit_usd: 2, password: "hidden-pass" },
    })] }),
    section({ kind: "deliveries", state: "ok", count: 1, items: [item({
      key: "delivery:1", title: "Check the chat", href: "#tasks/task-1?gate=s2",
      facts: { source: "catchup", channel: "lark", fix: "Check the chat" },
    })] }),
    section({ kind: "ledger", state: "ok", count: 1, items: [item({
      key: "ledger:9", title: "Nested task page", href: "#tasks/task-1/steps",
    })] }),
  ], 6);

  it("keeps server hrefs, rewrites only a bare task link, and exposes no write action", async () => {
    const call = vi.spyOn(v1, "callV1");
    const confirm = vi.spyOn(v1, "confirmV1");
    vi.mocked(fetchAttention).mockResolvedValue(paused);
    await render();
    expect(host.querySelector('a[href="#work/task/task-1"]')?.textContent).toBe("See #tasks/guessed-from-text");
    expect(host.querySelector('a[href="#tasks/guessed-from-text"]')).toBeNull();
    expect(host.querySelector('a[href="#schedules/sched-paused"]')).not.toBeNull();
    expect(host.querySelector('a[href="#settings/cost"]')).not.toBeNull();
    expect(host.querySelector('a[href="#tasks/task-1?gate=s2"]')).not.toBeNull();
    expect(host.querySelector('a[href="#tasks/task-1/steps"]')).not.toBeNull();
    expect(host.textContent).toContain("Login rewrite");
    expect(host.textContent).toContain("s2");
    expect(host.textContent).toContain("1 open question");
    expect(host.textContent).not.toContain("super-secret-token");
    expect(host.textContent).not.toContain("hidden-pass");
    expect(host.innerHTML).not.toContain('{"token"');
    const labels = [...host.querySelectorAll("button")].map((button) => button.textContent);
    expect(labels).not.toContain("恢复");
    expect(labels).not.toContain("确认");
    expect(labels).not.toContain("重发");
    expect(labels).not.toContain("开启");
    for (const button of host.querySelectorAll("button")) {
      await act(async () => { (button as HTMLButtonElement).click(); });
    }
    expect(call).not.toHaveBeenCalled();
    expect(confirm).not.toHaveBeenCalled();
    const link = host.querySelector('a[href="#work/task/task-1"]') as HTMLAnchorElement;
    link.focus();
    expect(document.activeElement).toBe(link);
    expect(link.tabIndex).toBeGreaterThanOrEqual(0);
    expect(link.className).toContain("focus-visible:ring-ring");
    call.mockRestore();
    confirm.mockRestore();
  });

  it("retries a failed source by reading the summary again", async () => {
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({ kind: "deliveries", state: "error", count: 0, error: "database down", items: [] }),
      section({ kind: "gates", state: "ok", count: 1, items: [item({ key: "g1", title: "Pick a database" })] }),
    ], 1));
    await render();
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({ kind: "deliveries", state: "ok", count: 0, items: [] }),
      section({ kind: "gates", state: "ok", count: 1, items: [item({ key: "g1", title: "Pick a database" })] }),
    ], 1));
    const button = host.querySelector('section[aria-label="Messages that may not have arrived"] button') as HTMLButtonElement;
    await act(async () => { button.click(); });
    expect(vi.mocked(fetchAttention).mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(host.querySelector('section[aria-label="Messages that may not have arrived"]')?.textContent).toContain("没有待办");
    expect(host.textContent).toContain("Pick a database");
  });
});

describe("Work attention polling", () => {
  beforeEach(() => { vi.useFakeTimers(); });

  it("polls once while visible, pauses while hidden, and stops after unmount", async () => {
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({ kind: "gates", state: "ok", count: 1, items: [item({ key: "g1", title: "Pick a database" })] }),
    ]));
    await render();
    expect(fetchAttention).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(14_999); });
    expect(fetchAttention).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(fetchAttention).toHaveBeenCalledTimes(2);

    await act(async () => { visibility("hidden"); await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchAttention).toHaveBeenCalledTimes(2);
    await act(async () => { visibility("visible"); await vi.advanceTimersByTimeAsync(0); });
    expect(fetchAttention).toHaveBeenCalledTimes(3);

    act(() => root.unmount());
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchAttention).toHaveBeenCalledTimes(3);
    root = createRoot(host);
  });

  it("does not start a second read while the first is in flight", async () => {
    vi.mocked(attentionSettingsGet).mockReturnValue(new Promise(() => {}));
    await render();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(attentionSettingsGet).toHaveBeenCalledTimes(1);
    expect(fetchAttention).not.toHaveBeenCalled();
  });

  it("does not read the summary while the setting stays off", async () => {
    vi.mocked(attentionSettingsGet).mockResolvedValue({ enabled: false });
    await render();
    await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
    expect(attentionSettingsGet).toHaveBeenCalledTimes(2);
    expect(fetchAttention).not.toHaveBeenCalled();
    expect(host.textContent).toContain("待办汇总未开启");
  });

  it("does not read while the tab is already hidden, then reads once it is visible", async () => {
    visibility("hidden");
    await render();
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(attentionSettingsGet).not.toHaveBeenCalled();
    await act(async () => { visibility("visible"); await vi.advanceTimersByTimeAsync(0); });
    expect(attentionSettingsGet).toHaveBeenCalledTimes(1);
  });
});

function stubFetch() {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (method === "GET") {
      return new Response(JSON.stringify({ status: "error", error: { code: "invalid", message: "use POST" } }), { status: 405 });
    }
    const path = String(url);
    const result = path.includes("operator_chat/messages")
      ? { messages: [{ message_id: "manager-message", role: "assistant", content: "Manager conversation stays", created_at: "2026-01-01T00:00:00Z" }] }
      : path.includes("tasks/detail")
      ? { task_id: "task-1", title: "Task body stays", status: "running", group: "in_progress", project_id: "p", project_name: "Demo", requirement: "" }
      : path.includes("tasks/activity")
        ? { activity: [{ seq: 1 }] }
        : { items: [], schedules: [], next_cursor: null };
    return new Response(JSON.stringify({ status: "ok", result }), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

describe("Work attention on the manager column", () => {
  it("drops a late manager summary when a task is selected and aborts the poll", async () => {
    stubFetch();
    let release: ((value: AttentionSummary) => void) | undefined;
    let signal: AbortSignal | undefined;
    vi.mocked(fetchAttention).mockImplementation((next?: AbortSignal) => {
      signal = next;
      return new Promise((resolve) => { release = resolve; });
    });
    await render(<WorkPage sub="manager" onNavigate={() => {}} />);
    expect(host.textContent).toContain("正在读取待办…");
    expect(host.querySelector('aside[aria-label="需要你处理"]')).not.toBeNull();
    expect(fetchAttention).toHaveBeenCalledTimes(1);
    await act(async () => { root.render(<WorkPage sub="task/task-1" onNavigate={() => {}} />); });
    expect(signal?.aborted).toBe(true);
    expect(host.querySelector('aside[aria-label="需要你处理"]')).toBeNull();
    await act(async () => {
      release?.(summary([
        section({ kind: "gates", state: "ok", count: 1, items: [item({ key: "g1", title: "Late manager item" })] }),
      ]));
    });
    expect(host.textContent).toContain("Task body stays");
    expect(host.textContent).not.toContain("Late manager item");
    expect(host.textContent).not.toContain("正在读取待办…");
    const calls = vi.mocked(fetchAttention).mock.calls.length;
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchAttention).toHaveBeenCalledTimes(calls);
  });

  it("shows the loading line before the first answer", async () => {
    vi.mocked(attentionSettingsGet).mockReturnValue(new Promise(() => {}));
    await render();
    expect(host.textContent).toContain("正在读取待办…");
    expect(host.textContent).not.toContain("暂时没有需要你处理的事");
  });

  it("keeps one Manager conversation beside the summary and gives both scroll space", async () => {
    stubFetch();
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({
        kind: "gates", state: "ok", count: 12,
        items: Array.from({ length: 12 }, (_, index) => item({ key: `g${index}`, title: `Waiting item ${index}` })),
      }),
    ]));
    await render(<WorkPage sub="manager" onNavigate={() => {}} />);
    const aside = host.querySelector('aside[aria-label="需要你处理"]');
    const column = aside?.parentElement;
    const manager = column?.children[0];
    const title = host.querySelector("header");
    const conversation = host.querySelector('section[aria-label="Conversation"]');
    const back = [...host.querySelectorAll("button")].find((button) => button.textContent === "Back to the list");
    const names = (node: Element | null | undefined) => (node?.className ?? "").split(/\s+/);
    // Below xl the conversation and list share the height. Both must shrink and scroll
    // so the live conversation cannot push the summary outside the viewport.
    expect(names(column)).toContain("flex-col");
    expect(names(aside)).toContain("min-h-0");
    expect(names(aside)).toContain("flex-1");
    expect(names(aside)).toContain("overflow-y-auto");
    expect(names(aside)).not.toContain("max-h-full");
    expect(names(aside)).not.toContain("shrink-0");
    expect(names(aside).some((name) => name === "xl:shrink-0" || name === "xl:flex-none")).toBe(true);
    expect(names(manager)).toContain("min-h-0");
    expect(names(manager)).toContain("flex-1");
    expect(names(title)).toContain("shrink-0");
    expect(conversation?.parentElement).toBe(manager);
    expect(conversation?.getAttribute("data-identity")).toMatch(/^manager:/);
    expect(conversation?.textContent).toContain("Manager conversation stays");
    expect(host.querySelectorAll('section[aria-label="Conversation"]')).toHaveLength(1);
    expect(host.querySelectorAll('textarea[aria-label="Message"]')).toHaveLength(1);
    expect(host.querySelector('a[href="#chat"]')).toBeNull();
    expect(names(back?.parentElement)).toContain("shrink-0");
  });
});

describe("Work attention login diagnosis", () => {
  it("shows a diagnosis that mentions a token and never renders a credential value", async () => {
    const credential = "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ12";
    vi.mocked(fetchAttention).mockResolvedValue(summary([
      section({ kind: "logins", state: "ok", count: 1, items: [item({
        key: "login:cursor", title: "Cursor login",
        facts: {
          detail: "partly signed in (no refresh token); refresh_token=rt_live_supersecretvalue",
          fix: `Run cursor-agent login; dropped ${credential}`,
          refresh_token: "rt_live_supersecretvalue",
        },
      })] }),
    ]));
    await render();
    expect(host.textContent).toContain("partly signed in (no refresh token)");
    expect(host.textContent).toContain("Run cursor-agent login");
    expect(host.textContent).not.toContain(credential);
    expect(host.innerHTML).not.toContain(credential);
    expect(host.textContent).not.toContain("rt_live_supersecretvalue");
    expect(host.innerHTML).not.toContain("rt_live_supersecretvalue");
  });
});

describe("Work attention narrow column", () => {
  function columnWidth(matches: boolean) {
    const listeners = new Set<() => void>();
    let wide = matches;
    vi.stubGlobal("matchMedia", () => ({
      get matches() { return wide; },
      media: "(min-width: 768px)",
      addEventListener: (_type: string, listener: () => void) => { listeners.add(listener); },
      removeEventListener: (_type: string, listener: () => void) => { listeners.delete(listener); },
    }));
    return {
      set(next: boolean) {
        wide = next;
        listeners.forEach((listener) => listener());
      },
    };
  }

  it("aborts the read and stops polling when returning to the list", async () => {
    columnWidth(false);
    stubFetch();
    let signal: AbortSignal | undefined;
    vi.mocked(fetchAttention).mockImplementation((next?: AbortSignal) => {
      signal = next;
      return new Promise(() => {});
    });
    await render(<WorkPage sub="manager" onNavigate={() => {}} />);
    expect(host.querySelector('aside[aria-label="需要你处理"]')).not.toBeNull();
    const back = [...host.querySelectorAll("button")].find((button) => button.textContent === "Back to the list");
    await act(async () => { back?.click(); });
    expect(signal?.aborted).toBe(true);
    expect(host.querySelector('aside[aria-label="需要你处理"]')).toBeNull();
    const calls = vi.mocked(fetchAttention).mock.calls.length;
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchAttention).toHaveBeenCalledTimes(calls);
  });

  it("remounts the summary when the detail column crosses the breakpoint", async () => {
    const media = columnWidth(false);
    stubFetch();
    const signals: AbortSignal[] = [];
    vi.mocked(fetchAttention).mockImplementation((next?: AbortSignal) => {
      if (next) signals.push(next);
      return Promise.resolve(summary([
        section({ kind: "gates", state: "ok", count: 1, items: [item({ key: "g1", title: "Pick a database" })] }),
      ]));
    });
    await render(<WorkPage sub="" onNavigate={() => {}} />);
    expect(host.querySelector('aside[aria-label="需要你处理"]')).toBeNull();
    expect(fetchAttention).not.toHaveBeenCalled();

    await act(async () => { media.set(true); });
    expect(host.querySelector('aside[aria-label="需要你处理"]')).not.toBeNull();
    expect(fetchAttention).toHaveBeenCalledTimes(1);
    expect(host.textContent).toContain("Pick a database");

    await act(async () => { media.set(false); });
    expect(host.querySelector('aside[aria-label="需要你处理"]')).toBeNull();
    expect(signals[0]?.aborted).toBe(true);
    const calls = vi.mocked(fetchAttention).mock.calls.length;
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(fetchAttention).toHaveBeenCalledTimes(calls);

    vi.useRealTimers();
    await act(async () => { media.set(true); });
    expect(fetchAttention).toHaveBeenCalledTimes(calls + 1);
    expect(host.textContent).toContain("Pick a database");
    expect(signals.at(-1)?.aborted).toBe(false);
  });
});

