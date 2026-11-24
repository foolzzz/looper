// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, expect, it, vi } from "vitest";
import { TaskFailureHolds } from "./task-failure-holds";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
afterEach(() => vi.unstubAllGlobals());
it("shows all holds, cancels without writing, confirms one version, and pages history", async () => {
  const writes: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    if (path.endsWith("hold_release")) { writes.push(body); return Response.json({ status: "needs_confirmation", confirmation_id: "confirm", expires_in_seconds: 600, impact: "Retry the reviewer" }, { status: 202 }); }
    if (path.endsWith("/confirm")) return Response.json({ status: "ok", result: { task_id: "task", step_id: "step", failure_id: 4, action: "retry", status: "running" } });
    if (path.endsWith("/holds")) return Response.json({ status: "ok", result: { holds: [hold] } });
    return Response.json({ status: "ok", result: { items: [{ id: body.after_id ? 5 : 4, step_identity: "step", step_key: "check", role: "reviewer", failure_class: "environment", failure_cause: "login", streak: 1, action: "hold", created_at: "2026-01-01T00:00:00Z", target: {} }], next_after_id: body.after_id ? null : 4 } });
  }));
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  const button = (text: string) => [...host.querySelectorAll<HTMLButtonElement>("button")].find(b => b.textContent === text)!;
  try {
    await act(async () => root.render(<TaskFailureHolds taskId="task" holds={[hold]} onChanged={() => {}} />));
    expect(host.textContent).toContain("login");
    await act(async () => button("已修好，重试").click());
    await act(async () => button("取消").click());
    expect(writes).toHaveLength(1); // preparing server confirmation does not release the hold
    expect(host.textContent).toContain("login");
    await act(async () => button("加载更多").click());
    expect(host.querySelectorAll('[data-failure-id]')).toHaveLength(2);
  } finally { act(() => root.unmount()); host.remove(); }
});
const hold = { step_id: "step", task_id: "task", kind: "environment" as const, cause: "login", provider_id: null,
  until: null, failure_id: 4, created_at: "2026-01-01T00:00:00Z", streak: 1, role: "reviewer", target: {}, targets: [], result: {}, run_id: "run", step_key: "check", reminded_at: null };

it("keeps multiple holds visible and refreshes a stale version without reporting empty history", async () => {
  const requests: Record<string, unknown>[] = [];
  let historyReads = 0;
  vi.stubGlobal("fetch", vi.fn(async (path: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    if (path.endsWith("/failure_history")) {
      historyReads++;
      return Response.json({ status: "error", error: { code: "unavailable", message: "历史服务不可用" } }, { status: 503 });
    }
    if (path.endsWith("/hold_release")) {
      requests.push(body);
      return Response.json({ status: "error", error: { code: "conflict", message: "版本已改变" } }, { status: 409 });
    }
    return Response.json({ status: "ok", result: { holds: [{ ...hold, failure_id: 6 }, { ...hold, step_id: "sibling", kind: "transient", until: "2026-01-01T00:02:00Z" }] } });
  }));
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  const changed = vi.fn();
  try {
    await act(async () => root.render(<TaskFailureHolds taskId="task" holds={[hold, { ...hold, step_id: "sibling", kind: "transient", until: "2026-01-01T00:02:00Z" }]} onChanged={changed} />));
    expect(host.textContent).toContain("2 个步骤");
    expect(host.querySelector('[role="alert"]')?.textContent).toContain("服务暂时不可用");
    expect(host.textContent).not.toContain("尚无失败历史");
    const retry = [...host.querySelectorAll<HTMLButtonElement>("button")].find(button => button.textContent === "已修好，重试")!;
    await act(async () => retry.click());
    expect(requests[0]).toMatchObject({ failure_id: 4 });
    expect(changed).toHaveBeenCalledOnce();
    expect(host.textContent).toContain("2 个步骤");
    await act(async () => retry.click());
    expect(requests[1]).toMatchObject({ failure_id: 6 });
    expect(historyReads).toBe(1);
  } finally { act(() => root.unmount()); host.remove(); }
});
