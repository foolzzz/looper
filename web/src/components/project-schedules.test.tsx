// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PlannerSessionStatus, ResetPlannerSession } from "@/components/project-schedules";
import type { ScheduleInfo } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const emptySession: ScheduleInfo["planner_session"] = {
  active: false, runtime: null, profile: null, last_task_id: null, last_task_title: null, updated_at: null,
};
const schedule = (active = true): ScheduleInfo => ({
  schedule_id: "sched/a", name: "Daily check", timezone: "America/Los_Angeles", overlap: "parallel",
  planner_session: active ? {
    active: true, runtime: "claude-code", profile: "profile-a", last_task_id: "task-a",
    last_task_title: "Daily check · previous run", updated_at: "2026-10-19T01:00:00Z",
  } : emptySession,
} as ScheduleInfo);

describe("schedule Planner session", () => {
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

  it("shows stored session status even for parallel, in the schedule's timezone, with its last task", async () => {
    await act(async () => root.render(<PlannerSessionStatus schedule={schedule()} />));
    expect(container.textContent).toContain("Planner session: continuing since");
    expect(container.textContent).toContain("Oct 18");
    expect(container.textContent).toContain("PDT");
    expect(container.textContent).toContain("last used by run Daily check · previous run");
    expect(container.querySelector("a")?.getAttribute("href")).toBe("#tasks/task-a");
    expect(container.textContent).not.toContain("profile-a");
  });

  it("shows none yet for an empty session", async () => {
    await act(async () => root.render(<PlannerSessionStatus schedule={schedule(false)} />));
    expect(container.textContent).toBe("Planner session: none yet");
  });

  it("asks before reset, leaves a cancelled reset alone, and refreshes after confirming", async () => {
    const fetch = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
      if (url === "/api/schedules/sched%2Fa/reset-planner-session") return Response.json({ cleared: true });
      if (url === "/api/v1/schedule_admin/get") return Response.json({ status: "ok", result: { schedule: schedule(false) } });
      throw new Error(`Unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetch);
    const confirm = vi.fn().mockReturnValueOnce(false).mockReturnValueOnce(true);
    vi.stubGlobal("confirm", confirm);
    const onReset = vi.fn();
    await act(async () => root.render(<ResetPlannerSession schedule={schedule()} onReset={onReset} />));
    const button = container.querySelector("button")!;
    await act(async () => button.click());
    expect(fetch).not.toHaveBeenCalled();
    await act(async () => button.click());
    expect(confirm.mock.calls[0][0]).toContain("next run starts a new session");
    expect(confirm.mock.calls[0][0]).toContain("will not write it back");
    expect(fetch.mock.calls[0][0]).toBe("/api/schedules/sched%2Fa/reset-planner-session");
    expect(fetch.mock.calls[0][1]?.method).toBe("POST");
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(fetch.mock.calls[1]).toEqual(["/api/v1/schedule_admin/get", expect.objectContaining({
      method: "POST", body: JSON.stringify({ schedule_id: "sched/a" }),
    })]);
    expect(onReset).toHaveBeenCalledWith(schedule(false));
  });

  it("allows reset without a stored session and shows a refused reset", async () => {
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.stubGlobal("fetch", vi.fn(async () => Response.json({ error: "not available" }, { status: 500 })));
    const onReset = vi.fn();
    await act(async () => root.render(<ResetPlannerSession schedule={schedule(false)} onReset={onReset} />));
    expect(container.querySelector("button")?.disabled).toBe(false);
    await act(async () => container.querySelector("button")!.click());
    expect(container.querySelector("[role=alert]")?.textContent).toContain("not available");
    expect(onReset).not.toHaveBeenCalled();
  });
});
