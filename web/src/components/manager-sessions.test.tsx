// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ManagerSessions } from "@/components/manager-sessions";
import { callV1 } from "@/lib/v1";

vi.mock("@/lib/v1", async (original) => ({
  ...await original<typeof import("@/lib/v1")>(),
  callV1: vi.fn(),
}));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const LIST = {
  routing_enabled: true,
  sessions: [
    { key: "main", kind: "main", name: "主会话", resident: true, last_active_at: "2026-11-22T10:00:00Z",
      context_tokens: null, cost_usd: 1.5 },
    { key: "group:channel-lark:oc_synthroom01", kind: "group", name: "Synthetic room", resident: false,
      last_active_at: "2026-11-22T09:00:00Z", context_tokens: null, cost_usd: 0.25 },
  ],
};

describe("Manager sessions", () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-22T10:00:20Z"));
    vi.resetAllMocks();
    vi.mocked(callV1).mockResolvedValue(LIST);
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.useRealTimers();
  });

  const show = async () => {
    await act(async () => { root.render(<ManagerSessions />); });
  };

  it("renders each session's name, kind, residency, activity, context and cost", async () => {
    await show();
    const text = container.textContent!;
    for (const part of ["主会话", "群", "Synthetic room", "常驻", "已回收", "不到 1 分钟前", "–", "$1.50", "$0.2500"]) {
      expect(text).toContain(part);
    }
    expect(text).not.toContain("按会话分开是关的");
  });

  it("asks before reset and then calls the operation", async () => {
    await show();
    const row = [...container.querySelectorAll("tr")].find((tr) => tr.textContent?.includes("Synthetic room"))!;
    await act(async () => row.querySelector("button")!.click());
    expect(container.querySelector("[role=dialog]")!.textContent).toContain(
      "重置这个会话？管家会忘掉这里之前的对话，下一条消息开一个新会话。");
    expect(vi.mocked(callV1).mock.calls.map((call) => call[0])).toEqual(["/api/v1/manager_sessions/list"]);
    vi.mocked(callV1).mockResolvedValueOnce({ ...LIST.sessions[1], resident: false, cost_usd: null });
    const confirm = [...container.querySelectorAll("button")].find(
      (button) => button.closest("[role=dialog]") !== null && button.textContent === "重置") as HTMLButtonElement;
    await act(async () => confirm.click());
    expect(vi.mocked(callV1).mock.calls[1]).toEqual([
      "/api/v1/manager_sessions/reset", { key: "group:channel-lark:oc_synthroom01" }]);
  });

  it("shows only main and a note when per-chat sessions are off", async () => {
    vi.mocked(callV1).mockResolvedValue({ ...LIST, routing_enabled: false });
    await show();
    const text = container.textContent!;
    expect(text).toContain("按会话分开是关的，这里只显示主会话。");
    expect(text).toContain("主会话");
    expect(text).not.toContain("Synthetic room");
  });
});
