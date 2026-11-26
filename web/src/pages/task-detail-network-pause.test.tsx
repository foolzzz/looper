// @vitest-environment happy-dom
import { act } from "react";
import { type Root, createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetailRoute } from "@/pages/task-detail";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("task network pause", () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  const show = async (networkPause: Record<string, unknown> | undefined) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/tasks/detail") return Response.json({ status: "ok", result: {
        task_id: "task-network", name: "Parser", requirement: "Build a parser.", status: "stopped", group: "attention",
        phase: "stopped", created_at: "2026-10-17T08:00:00Z", updated_at: "2026-10-17T08:01:00Z",
        project_id: "project", project_name: "Parser", work_dir: "/workspace", config: {}, cost: null,
        steps: [], journal: [], gates: [], roles: [], supplements: [], profile_overrides: {},
        network_pause: networkPause,
      } });
      if (url === "/api/v1/tasks/activity") return Response.json({ status: "ok", result: { activity: [] } });
      if (url === "/api/v1/tasks/conversation") return Response.json({ status: "ok", result: { rows: [], has_more: false } });
      return Response.json({});
    }));
    await act(async () => root.render(<TaskDetailRoute taskId="task-network" onBack={() => {}} />));
    for (let i = 0; i < 5; i++) await act(async () => new Promise((resolve) => setTimeout(resolve, 0)));
  };

  it.each([
    ["paused", "网络暂停", "连接恢复后自动继续"],
    ["waiting_user", "等待人工处理", "网络暂停已达到时限"],
  ])("shows %s reason across tabs", async (state, label, action) => {
    await show({ id: 1, state, reason: "Model API unavailable (ENOTFOUND)",
      started_at: "2026-10-17T08:00:00Z", deadline: "2026-10-17T10:00:00Z" });
    const notice = () => container.querySelector('[aria-label="网络暂停状态"]');
    expect(notice()?.textContent).toContain(label);
    expect(notice()?.textContent).toContain("Model API unavailable (ENOTFOUND)");
    expect(notice()?.textContent).toContain(action);
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>("[role='tab']")]
      .find(tab => tab.textContent?.trim() === "Conversation")!.click());
    expect(notice()?.textContent).toContain("Model API unavailable (ENOTFOUND)");
  });

  it("adds nothing for tasks without a network pause", async () => {
    await show(undefined);
    expect(container.querySelector('[aria-label="网络暂停状态"]')).toBeNull();
  });
});
