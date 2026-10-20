// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WaitPanel } from "@/components/task-waits";
import type { TaskWait } from "@/lib/api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const wait = (extra: Partial<TaskWait> = {}): TaskWait => ({
  step: "w1", step_id: "step-w1", state: "waiting", outcome: null, topic: "Wait for the PR", check: null,
  every_seconds: null, opened_at: "2026-10-20T08:00:00Z", due_at: "2026-10-21T08:00:00Z",
  deadline_at: "2026-10-27T08:00:00Z", next_check_at: null, last_checked_at: null,
  checks: 0, last_result: null, last_output: "", last_error: "", resolution: "", notice: null,
  closed_at: null, depends_on: [], ...extra,
});

describe("wait panel", () => {
  let container: HTMLDivElement;
  let root: Root;
  const changed = vi.fn();
  beforeEach(() => {
    changed.mockReset();
    container = document.createElement("div");
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });
  const show = (item: TaskWait) => act(async () => root.render(<WaitPanel taskId="task-a" wait={item} onChanged={changed} />));
  const button = (text: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === text)!;

  it.each<TaskWait["state"]>(["pending", "waiting", "ready", "timed_out", "cancel_requested", "done", "failed", "skipped"])(
    "offers actions only while waiting (%s)", async (state) => {
      await show(wait({ state }));
      expect(Boolean(button("Resume now"))).toBe(state === "waiting");
      expect(Boolean(button("Cancel"))).toBe(state === "waiting");
      if (state === "timed_out" || state === "cancel_requested") {
        expect(container.textContent).toContain("The Planner decides what happens next");
      }
    },
  );

  it("shows checks, timing, errors and escaped output in a closed disclosure", async () => {
    await show(wait({ check: { type: "command", command: "test -f ready" }, every_seconds: 600,
      checks: 2, last_result: "error", last_error: "exit 1", last_output: "<img src=x onerror=alert(1)>" }));
    expect(container.querySelector("summary")?.textContent).toContain("Waiting");
    expect(container.textContent).toContain("Command `test -f ready`");
    expect(container.textContent).toContain("Every 10 min");
    expect(container.textContent).toContain("Earliest resume");
    expect(container.querySelectorAll("time")).toHaveLength(3);
    expect(container.textContent).toContain("exit 1");
    expect(container.querySelector("pre")?.textContent).toBe("<img src=x onerror=alert(1)>");
    expect(container.querySelector("pre")?.closest("details")?.open).toBe(false);
    expect(container.querySelector("img")).toBeNull();
  });

  it.each(["sent", "dashboard", "failed"] as const)("keeps the timeout notice result (%s)", async (notice) => {
    await show(wait({ state: "timed_out", notice, resolution: "Check did not become ready" }));
    expect(container.textContent).toContain({ sent: "Notice sent", dashboard: "Dashboard", failed: "Failed" }[notice]);
    expect(container.textContent).toContain("Check did not become ready");
  });

  it("disables both actions until resume finishes, then refreshes the task", async () => {
    let release!: (value: Response) => void;
    const fetch = vi.fn(() => new Promise<Response>((resolve) => { release = resolve; }));
    vi.stubGlobal("fetch", fetch);
    await show(wait());
    await act(async () => button("Resume now").click());
    expect(button("Resume now").disabled).toBe(true);
    expect(button("Cancel").disabled).toBe(true);
    expect(fetch.mock.calls[0]).toMatchObject(["/api/tasks/task-a/waits/w1/resume", { method: "POST" }]);
    await act(async () => release(Response.json({ task_id: "task-a", wait: wait({ state: "ready", outcome: "resumed" }) })));
    expect(changed).toHaveBeenCalledOnce();
  });

  it("confirms cancellation and shows the API's conflict text without refreshing", async () => {
    const reason = "wait w1 is timed_out: its Planner decides what happens next";
    const fetch = vi.fn(async () => Response.json({ error: reason }, { status: 409 }));
    vi.stubGlobal("fetch", fetch);
    await show(wait());
    await act(async () => button("Cancel").click());
    expect(fetch).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(button("Keep waiting"));
    await act(async () => button("Keep waiting").click());
    expect(document.activeElement).toBe(button("Cancel"));
    await act(async () => button("Cancel").click());
    await act(async () => button("Confirm cancel").click());
    expect(fetch.mock.calls[0]).toMatchObject(["/api/tasks/task-a/waits/w1/cancel", { method: "POST" }]);
    expect(container.querySelector("[role=alert]")?.textContent).toContain(reason);
    expect(changed).not.toHaveBeenCalled();
    expect(button("Confirm cancel").disabled).toBe(false);
  });
});
