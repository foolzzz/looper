// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NeedsYouPage } from "@/pages/needs-you";
import { fetchAttention, type AttentionSummary } from "@/lib/api-attention";
import { V1Error } from "@/lib/v1";

vi.mock("@/lib/api-attention", async (original) => ({
  ...await original<typeof import("@/lib/api-attention")>(), fetchAttention: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const summary: AttentionSummary = {
  read_at: "2026-11-20T08:00:00Z", total: 4, sections: [
    { kind: "gates", state: "ok", count: 2, error: null, items: [
      { key: "gate:t1:s2", title: "Pick a database", context: "Login rewrite", since: "2026-11-20T05:00:00Z",
        href: "#tasks/t1?gate=s2", ref: null, facts: { step: "s2", questions: 1 } }] },
    { kind: "confirmations", state: "unknown", count: 0, error: null, items: [] },
    { kind: "schedules", state: "ok", count: 0, error: null, items: [] },
    { kind: "deliveries", state: "error", count: 0, error: "ConnectionError: down", items: [] },
    { kind: "ledger", state: "ok", count: 1, error: null, items: [
      { key: "ledger:3", title: "Write the report", context: "", since: null, href: null, ref: "L-0003",
        facts: { kind: "work_item", overdue: true, due_at: null } }] },
    { kind: "logins", state: "ok", count: 1, error: null, items: [
      { key: "login:claude_login", title: "Claude login", context: "", since: null, href: "#settings/runtimes",
        ref: null, facts: { detail: "not logged in", fix: "claude auth login" } }] },
  ],
};

let root: Root;
let host: HTMLElement;

async function render() {
  await act(async () => { root.render(<NeedsYouPage />); });
}

beforeEach(() => {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.clearAllMocks();
});

describe("Needs you page", () => {
  it("lists spend holds with their cap and a direct link to continue or stop", async () => {
    vi.mocked(fetchAttention).mockResolvedValue({ read_at: "2026-11-20T08:00:00Z", total: 1, sections: [
      { kind: "cost", state: "ok", count: 1, error: null, items: [
        { key: "cost:1", title: "任务花费等你决定", context: "demo", since: null,
          href: "#settings/cost", ref: "C-1", facts: { currency: "provider_priced_usd", spent_usd: 2, limit_usd: 2 } }] },
    ] });
    await render();
    const section = host.querySelector('section[aria-label="花费等你决定"]')!;
    expect(section.textContent).toContain("C-1");
    expect(section.textContent).toContain("$2.00");
    expect(section.querySelector("a")?.getAttribute("href")).toBe("#settings/cost");
  });

  it("lists what waits, says what could not be read, and folds the empty sections into one line", async () => {
    vi.mocked(fetchAttention).mockResolvedValue(summary);
    await render();
    const gates = host.querySelector('section[aria-label="Gates waiting for your answer"]')!;
    expect(gates.querySelector("a")?.getAttribute("href")).toBe("#tasks/t1?gate=s2");
    expect(gates.textContent).toContain("Login rewrite · s2 · 1 open question");
    expect(gates.textContent).toContain("and 1 more");
    const failed = host.querySelector('section[aria-label="Messages that may not have arrived"] [role="alert"]');
    expect(failed?.textContent).toContain("ConnectionError: down");
    const ledger = host.querySelector('section[aria-label="Ledger items waiting for you"]')!;
    expect(ledger.textContent).toContain("L-0003");
    expect(ledger.textContent).toContain("overdue");
    expect(host.querySelector('section[aria-label="Failing schedules"]')).toBeNull();
    expect(host.textContent).toContain("Nothing in: Failing schedules.");
    expect(host.textContent).toContain("Not known on this server: Operations to confirm.");
    // Failing logins are the banner's: no section here, and not counted.
    expect(host.textContent).not.toContain("Logins that fail");
    expect(host.textContent).toContain("3 things wait for you");
  });

  it("keeps a logins section that could not be read, and never says nothing waits while a source is unread",
    async () => {
      vi.mocked(fetchAttention).mockResolvedValue({ read_at: "2026-11-20T08:00:00Z", total: 0, sections: [
        { kind: "gates", state: "ok", count: 0, error: null, items: [] },
        { kind: "logins", state: "error", count: 0, error: "TimeoutError: checks", items: [] },
        { kind: "confirmations", state: "unknown", count: 0, error: null, items: [] },
      ] });
      await render();
      expect(host.querySelector('section[aria-label="Logins that fail"] [role="alert"]')?.textContent)
        .toContain("TimeoutError: checks");
      expect(host.textContent).not.toContain("Nothing waits for you");
      expect(host.textContent).toContain("2 sources could not be read");
      expect(host.textContent).toContain("Not known on this server: Operations to confirm.");
    });

  it("says nothing waits only when every source was read and is empty", async () => {
    vi.mocked(fetchAttention).mockResolvedValue({ read_at: "2026-11-20T08:00:00Z", total: 0, sections: [
      { kind: "gates", state: "ok", count: 0, error: null, items: [] },
      { kind: "logins", state: "ok", count: 0, error: null, items: [] },
    ] });
    await render();
    expect(host.textContent).toContain("Nothing waits for you");
  });

  it("says when the summary is off", async () => {
    vi.mocked(fetchAttention).mockRejectedValue(new V1Error(409, "conflict", "not enabled"));
    await render();
    expect(host.querySelector('[role="status"]')?.textContent).toContain("attention.enabled");
    expect(host.querySelector("section")).toBeNull();
  });

  it("shows a failed read with a retry", async () => {
    vi.mocked(fetchAttention).mockRejectedValueOnce(new Error("network down")).mockResolvedValue(summary);
    await render();
    const alert = host.querySelector('[role="alert"]')!;
    expect(alert.textContent).toContain("network down");
    await act(async () => { (alert.querySelector("button") as HTMLButtonElement).click(); });
    expect(host.querySelector('section[aria-label="Gates waiting for your answer"]')).not.toBeNull();
  });
});
