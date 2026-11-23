// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CostGuardSettingsSection } from "@/components/cost-guard-settings";
import { isSettingsPage, settingsPageLabel } from "@/lib/settings-pages";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("花费观察", () => {
  let container: HTMLDivElement;
  let root: Root;
  let result: Record<string, unknown>;
  let unavailable: boolean;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
    unavailable = false;
    result = { mode: "off", daily_usd: null, daily_equivalent_usd: null, roles: {}, problem: null,
      counters: [], recent: [], recommended_daily_usd: null, recommended_daily_equivalent_usd: null };
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      expect(String(input)).toBe("/api/v1/cost_guard/get");
      return unavailable
        ? Response.json({ status: "error", error: { code: "unavailable", message: "观察数据暂不可用" } }, { status: 503 })
        : Response.json({ status: "ok", result });
    }));
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function render() {
    await act(async () => root.render(<CostGuardSettingsSection />));
  }

  it("默认关闭、未设上限，并且没有编辑控件", async () => {
    await render();
    expect(isSettingsPage("cost")).toBe(true);
    expect(settingsPageLabel("cost")).toBe("花费");
    expect(container.textContent).toContain("已关闭");
    expect(container.textContent).toContain("未设上限");
    expect(container.textContent).toContain("先观察一周");
    expect(container.querySelectorAll("input,select,button,textarea")).toHaveLength(0);
    expect(container.textContent).toContain("暂无观察记录");
  });

  it("分钱种展示已花和预留，并列出本来会被拦下的记录", async () => {
    result = { ...result, mode: "observe", daily_usd: 2, recommended_daily_usd: 3,
      counters: [{ scope: "task", scope_ref: "demo", currency: "provider_priced_usd", spent_usd: 0.25,
        reserved_usd: 2, limit_usd: 2 }],
      recent: [{ run_kind: "worker", currency: "provider_priced_usd", estimate_usd: 1,
        estimate_source: "run_cap", decision: "ask", scope: "task", scope_ref: "demo", action: "ask" }] };
    await render();
    expect(container.textContent).toContain("仅观察");
    expect(container.textContent).toContain("$0.25");
    expect(container.textContent).toContain("$2.00");
    expect(container.textContent).toContain("worker");
    expect(container.textContent).toContain("需询问");
    expect(container.textContent).toContain("$3.00");
  });

  it("读取失败展示错误而不是成功或空数据", async () => {
    unavailable = true;
    await render();
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("观察数据暂不可用");
    expect(container.textContent).not.toContain("暂无观察记录");
  });
});
