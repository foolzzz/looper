// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LeadsSettings } from "@/components/leads-settings";
import { intentsSettingsGet, intentsSettingsPut, leadsSettingsGet, leadsSettingsPut } from "@/lib/api-leads";

vi.mock("@/lib/api-leads", () => ({
  leadsSettingsGet: vi.fn(), leadsSettingsPut: vi.fn(), intentsSettingsGet: vi.fn(), intentsSettingsPut: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const leads = {
  enabled: true, valid_days: 14,
  subscriptions: [{ schedule_id: "sched-leads", name: "消息线索", enabled: true, project_id: "project-a" }],
};
const intents = {
  enabled: true,
  subscriptions: [{ schedule_id: "sched-intents", name: "意图识别", enabled: true, project_id: "project-a", target_ok: false }],
};

describe("leads and intents settings", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.mocked(leadsSettingsGet).mockResolvedValue(leads);
    vi.mocked(intentsSettingsGet).mockResolvedValue(intents);
    vi.mocked(leadsSettingsPut).mockResolvedValue({ ...leads, enabled: false });
    vi.mocked(intentsSettingsPut).mockResolvedValue({ ...intents, enabled: false });
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.clearAllMocks();
  });

  const show = async () => { await act(async () => { root.render(<LeadsSettings />); }); };
  const switchOf = (name: string) => container.querySelector<HTMLButtonElement>(`[role="switch"][aria-label="${name}"]`)!;

  it("names the subscriptions before turning one off", async () => {
    await show();
    expect(container.textContent).toContain("消息线索 · 已启用");
    expect(container.textContent).toContain("意图识别 · 已启用 · 目标未确认");
    await act(async () => switchOf("消息线索").click());
    expect(leadsSettingsPut).not.toHaveBeenCalled();
    const dialog = container.querySelector("[role=dialog]")!;
    expect(dialog.textContent).toContain("消息线索");
    expect(dialog.textContent).not.toContain("意图识别");
    await act(async () => [...dialog.querySelectorAll("button")].find((item) => item.textContent === "关闭")!.click());
    expect(leadsSettingsPut).toHaveBeenCalledWith({ enabled: false });
  });

  it("turns a switch on without asking", async () => {
    vi.mocked(leadsSettingsGet).mockResolvedValue({ ...leads, enabled: false });
    await show();
    await act(async () => switchOf("消息线索").click());
    expect(container.querySelector("[role=dialog]")).toBeNull();
    expect(leadsSettingsPut).toHaveBeenCalledWith({ enabled: true });
  });
});
