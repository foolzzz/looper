// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MetricsAlertsControl } from "./metrics-alerts-control";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const settings = (enabled = false, last_checked: string | null = null) => ({ enabled, last_checked, rules: [] });
const ok = (result: unknown) => Response.json({ status: "ok", result });
const pending = () => Response.json({ status: "needs_confirmation", confirmation_id: "alert-confirmation", expires_in_seconds: 60,
  impact: "开启后每天向告警会话发送质量告警；送达层开启时遵守免打扰。" }, { status: 202 });

describe("quality alert switch", () => {
  let container: HTMLDivElement;
  let root: Root;
  let fetcher: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    fetcher = vi.fn().mockImplementation(async () => ok(settings()));
    vi.stubGlobal("fetch", fetcher);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });
  const show = async () => act(async () => root.render(<MetricsAlertsControl />));
  const toggle = () => container.querySelector('[role="switch"]') as HTMLButtonElement;
  const click = async (label: string) => {
    const button = [...container.querySelectorAll("button")].find((item) => item.textContent === label)!;
    await act(async () => button.click());
  };

  it("shows the off switch, its effect and the local last-check date", async () => {
    fetcher.mockResolvedValue(ok(settings(false, "2026-11-23")));
    await show();
    expect(toggle().getAttribute("aria-checked")).toBe("false");
    expect(container.textContent).toContain("每天检查质量回退，触发时向告警会话提醒");
    expect(container.textContent).toContain("免打扰");
    expect(container.textContent).toContain("2026-11-23");
    expect(fetcher.mock.calls[0][0]).toBe("/api/v1/metrics_alerts/get");
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({});
  });

  it("waits for explicit confirmation, preserves its session and updates the switch only after success", async () => {
    fetcher.mockResolvedValueOnce(ok(settings())).mockResolvedValueOnce(pending()).mockResolvedValueOnce(ok(settings(true)));
    await show();
    await act(async () => toggle().click());
    expect(fetcher.mock.calls[1][0]).toBe("/api/v1/metrics_alerts/update");
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ enabled: true });
    expect(toggle().getAttribute("aria-checked")).toBe("false");
    expect(toggle().disabled).toBe(true);
    expect(container.textContent).toContain("开启后每天向告警会话发送质量告警");
    expect(fetcher).toHaveBeenCalledTimes(2);
    await click("确认");
    expect(fetcher.mock.calls[2][0]).toBe("/api/v1/confirm");
    expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ confirmation_id: "alert-confirmation" });
    expect(fetcher.mock.calls[2][1].headers["X-Looper-Session"]).toBe(fetcher.mock.calls[1][1].headers["X-Looper-Session"]);
    expect(toggle().getAttribute("aria-checked")).toBe("true");
    expect(container.querySelector('[aria-label="确认质量告警设置"]')).toBeNull();
  });

  it("cancels without changing the setting or confirming the operation", async () => {
    fetcher.mockResolvedValueOnce(ok(settings())).mockResolvedValueOnce(pending());
    await show();
    await act(async () => toggle().click());
    await click("取消");
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(toggle().getAttribute("aria-checked")).toBe("false");
    expect(toggle().disabled).toBe(false);
  });

  it("keeps the current setting and shows the error when updating fails", async () => {
    fetcher.mockResolvedValueOnce(ok(settings(true))).mockResolvedValueOnce(Response.json({ status: "error",
      error: { code: "unavailable", message: "保存失败" } }, { status: 503 }));
    await show();
    await act(async () => toggle().click());
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ enabled: false });
    expect(toggle().getAttribute("aria-checked")).toBe("true");
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("保存失败");
  });

  it("disables the switch on a failed read, then allows a retry", async () => {
    fetcher.mockRejectedValueOnce(new Error("读取失败")).mockResolvedValueOnce(ok(settings()));
    await show();
    expect(toggle().disabled).toBe(true);
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("读取失败");
    await click("重试");
    expect(toggle().disabled).toBe(false);
    expect(container.textContent).toContain("尚未检查");
  });
});
